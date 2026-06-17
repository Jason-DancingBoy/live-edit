"""FastAPI router for live-edit endpoints."""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Header, Query, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from pydantic import BaseModel

from .config import parse_config
from .engine import (
    EditSession,
    SessionStore,
    build_timeline,
    run_edit_session,
    continue_edit_session,
    translate_error,
)
from .preview import PreviewManager
from .provider import AnthropicCompatibleProvider, Provider
from .storage import SQLiteStorage, Storage
from .vcs import GitVCS, VCS

logger = logging.getLogger("live-edit.router")


class StreamRequest(BaseModel):
    request: str
    mode: str = "quick"


class ContinueRequest(BaseModel):
    request: str
    mode: str = "quick"


class ApproveRequest(BaseModel):
    approved: bool = True


def _resolve_api_key(config) -> str:
    """Resolve API key from environment variable named in config."""
    env_var = getattr(config.llm, 'api_key_env', '') if hasattr(config, 'llm') else ''
    return os.environ.get(env_var, "")


def setup_live_edit(
    project_root: str = ".",
    config_path: str = ".live-edit.toml",
    provider: Provider | None = None,
    storage: Storage | None = None,
    vcs: VCS | None = None,
    api_key: str = "",
    admin_key: str = "",
) -> APIRouter:
    """Create and return a FastAPI router with all live-edit endpoints.

    Args:
        project_root: Root directory of the target project.
        config_path: Path to .live-edit.toml (relative or absolute).
        provider: Optional LLM provider override.
        storage: Optional storage override.
        vcs: Optional VCS override.
        api_key: API key override (takes priority over env var).
    """
    router = APIRouter(prefix="/live-edit", tags=["live-edit"])

    # Load config
    resolved_config_path = os.path.join(project_root, config_path) if not os.path.isabs(config_path) else config_path
    config = parse_config(resolved_config_path)

    # Resolve dependencies (injected > config > default)
    api_key = api_key or _resolve_api_key(config)
    if provider is None:
        provider = AnthropicCompatibleProvider(
            api_url=config.llm.api_url,
            api_key=api_key,
            model=config.llm.model,
        )
    if storage is None:
        db_path = os.path.join(project_root, "live_edit.db")
        storage = SQLiteStorage(db_path)
    if vcs is None:
        vcs = GitVCS(project_root)

    # Global session store
    ttl = getattr(config.timeouts, 'session_ttl', 1800) if hasattr(config, 'timeouts') else 1800
    max_active = getattr(config.sessions, 'max_active', 10) if hasattr(config, 'sessions') else 10
    session_store = SessionStore(max_active=max_active, ttl_seconds=ttl)

    # Preview manager (per-session preview services)
    preview_manager = PreviewManager(config.preview)

    # Static files directory (within the package)
    _static_dir = os.path.join(os.path.dirname(__file__), "static")

    # ── POST /live-edit/stream ──

    @router.post("/stream")
    async def start_stream(req: StreamRequest):
        """Start a new live-edit session with SSE streaming."""
        session_id = f"le_{uuid.uuid4().hex[:12]}"
        session = EditSession(session_id, req.request)

        if not session_store.add(session):
            raise HTTPException(status_code=503, detail="会话数已达上限，请稍后再试")

        mode = req.mode or getattr(config.ui, 'default_mode', 'quick')

        async def event_generator() -> AsyncIterator[str]:
            # Emit session event so frontend knows the session ID
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"

            # Run the session in background
            task = asyncio.ensure_future(
                run_edit_session(
                    session=session,
                    provider=provider,
                    vcs=vcs,
                    storage=storage,
                    config=config,
                    mode=mode,
                    preview_manager=preview_manager,
                    session_store=session_store,
                )
            )

            try:
                while True:
                    try:
                        event = await asyncio.wait_for(session.queue.get(), timeout=180.0)
                    except asyncio.TimeoutError:
                        yield f"data: {json.dumps({'type': 'error', 'error': '会话超时'})}\n\n"
                        break

                    if event is None:
                        break

                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                # If client disconnects, cancel the backend task
                if not session._done:
                    session.cancel()
                if not task.done():
                    try:
                        await asyncio.wait_for(task, timeout=30.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass

            await task

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── POST /live-edit/continue/{session_id} ──

    @router.post("/continue/{session_id}")
    async def continue_stream(session_id: str, req: ContinueRequest):
        """Continue an existing live-edit session."""
        session = session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")

        session.new_stream_queue()
        mode = req.mode or session._mode

        async def event_generator() -> AsyncIterator[str]:
            task = asyncio.ensure_future(
                continue_edit_session(
                    session=session,
                    new_request=req.request,
                    provider=provider,
                    vcs=vcs,
                    storage=storage,
                    config=config,
                    mode=mode,
                    preview_manager=preview_manager,
                    session_store=session_store,
                )
            )

            try:
                while True:
                    try:
                        event = await asyncio.wait_for(session.queue.get(), timeout=180.0)
                    except asyncio.TimeoutError:
                        yield f"data: {json.dumps({'type': 'error', 'error': '会话超时'})}\n\n"
                        break

                    if event is None:
                        break

                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                if not session._done:
                    session.cancel()
                if not task.done():
                    try:
                        await asyncio.wait_for(task, timeout=30.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass

            await task

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── POST /live-edit/approve/{session_id}/{tool_id} ──

    @router.post("/approve/{session_id}/{tool_id}")
    async def approve_tool(session_id: str, tool_id: str, req: ApproveRequest):
        """Approve or reject a tool execution (for quick mode)."""
        session = session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")
        session.approve(tool_id, req.approved)
        return {"ok": True}

    # ── POST /live-edit/cancel/{session_id} ──

    @router.post("/cancel/{session_id}")
    async def cancel_session(session_id: str):
        """Cancel a running live-edit session."""
        session = session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")
        session.cancel()
        logger.info("Session %s cancelled by user", session_id)
        return {"ok": True}

    # ── GET /live-edit/timeline ──

    @router.get("/timeline")
    async def get_timeline(limit: int = Query(default=30, le=100),
                           diff_for: str = Query(default="")):
        """Get the live-edit timeline (merged VCS commits + storage sessions).

        Optional: ?diff_for=<commit_hash> returns git show for that commit.
        """
        if diff_for:
            result = vcs.show_commit(diff_for)
            return result
        try:
            entries = build_timeline(vcs, storage, limit=limit)

            # Prepend root commit for frontend compatibility
            try:
                import subprocess
                r = subprocess.run(
                    ["git", "rev-list", "--max-parents=0", "HEAD"],
                    capture_output=True, text=True, timeout=10,
                    cwd=project_root,
                )
                root_hash = r.stdout.strip()[:8]
                info = subprocess.run(
                    ["git", "log", "-1", "--format=%s|%ai", root_hash],
                    capture_output=True, text=True, timeout=10,
                    cwd=project_root,
                )
                parts = info.stdout.strip().split("|", 1)
                entries.insert(0, {
                    "commit_hash": root_hash,
                    "message": parts[0] if parts else "Initial commit",
                    "date": parts[1] if len(parts) > 1 else "",
                    "is_initial": True,
                    "is_live_edit": False,
                    "session": None,
                })
            except Exception:
                pass

            return {"entries": entries}
        except Exception as e:
            logger.error("Timeline error: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ── GET /live-edit/history ──

    @router.get("/history")
    async def get_history(limit: int = Query(default=20, le=100)):
        """Get recent session history."""
        sessions = storage.get_sessions(limit=limit)
        return {"sessions": sessions}

    # ── GET /live-edit/session/{session_id} ──

    @router.get("/session/{session_id}")
    async def get_session_detail(session_id: str):
        """Get detailed info about a past session."""
        detail = storage.get_session_detail(session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return detail

    # ── POST /live-edit/revert/{commit_hash}/preview ──

    @router.post("/revert/{commit_hash}/preview")
    async def revert_preview(commit_hash: str):
        """Dry-run revert to check for conflicts."""
        preview = vcs.revert_preview(commit_hash)
        return {
            "ok": preview.ok,
            "can_revert": preview.can_revert,
            "files": preview.files,
            "diff_summary": preview.diff_summary,
            "conflicts": preview.conflicts,
            "error": preview.error,
        }

    # ── POST /live-edit/revert/{commit_hash}/execute ──

    @router.post("/revert/{commit_hash}/execute")
    async def revert_execute(commit_hash: str):
        """Execute revert and run post_revert hook if configured."""
        result = vcs.revert_execute(commit_hash)
        if result.ok and hasattr(config.hooks, 'post_revert') and config.hooks.post_revert:
            import subprocess
            try:
                subprocess.run(
                    config.hooks.post_revert, shell=True,
                    capture_output=True, timeout=30, cwd=project_root,
                )
            except Exception as e:
                logger.warning("post_revert hook failed: %s", e)
        return {
            "ok": result.ok,
            "new_commit_hash": result.new_commit_hash,
            "message": result.message,
            "error": result.error,
        }

    # ── GET /live-edit/static/{filename} ──

    @router.get("/static/{filename:path}")
    async def serve_static(filename: str):
        """Serve static frontend files."""
        # First check package static dir, then project's live_edit/static dir
        package_path = os.path.join(_static_dir, filename)
        project_static = os.path.join(project_root, "live_edit", "static", filename)

        for p in [package_path, project_static]:
            if os.path.isfile(p):
                return FileResponse(p)

        raise HTTPException(status_code=404, detail=f"Static file not found: {filename}")

    # ── GET /live-edit/health ──

    @router.get("/health")
    async def health():
        """Health check endpoint."""
        return {
            "status": "ok",
            "active_sessions": session_store.count,
        }

    # ── Preview reverse proxy (routes to session's uvicorn on 127.0.0.1) ──

    @router.api_route("/p/{session_id}/{rest:path}",
                      methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
    async def proxy_preview_root(session_id: str, rest: str, request: Request):
        """Proxy requests to the session's preview uvicorn instance."""
        session = session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")

        internal_url = preview_manager.get_url(session_id)
        if not internal_url:
            raise HTTPException(status_code=404, detail="预览服务未运行")

        target = f"{internal_url}/{rest}"
        if request.url.query:
            target += f"?{request.url.query}"

        body = await request.body()

        # Forward headers, filtering hop-by-hop
        fwd_headers = {}
        for key, value in request.headers.items():
            low = key.lower()
            if low in ("host", "connection", "transfer-encoding"):
                continue
            fwd_headers[key] = value

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method=request.method,
                    url=target,
                    headers=fwd_headers,
                    content=body,
                )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="预览服务无法连接")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="预览服务响应超时")

        # Build response, filtering hop-by-hop response headers
        resp_headers = {}
        for key, value in resp.headers.items():
            low = key.lower()
            if low in ("transfer-encoding", "connection", "keep-alive"):
                continue
            resp_headers[key] = value

        return StreamingResponse(
            resp.aiter_bytes(),
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type", "application/octet-stream"),
        )

    @router.api_route("/p/{session_id}",
                      methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
    async def proxy_preview(session_id: str, request: Request):
        """Proxy root path to the session's preview uvicorn instance."""
        session = session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")

        internal_url = preview_manager.get_url(session_id)
        if not internal_url:
            raise HTTPException(status_code=404, detail="预览服务未运行")

        target = internal_url
        if request.url.query:
            target += f"?{request.url.query}"

        body = await request.body()

        fwd_headers = {}
        for key, value in request.headers.items():
            low = key.lower()
            if low in ("host", "connection", "transfer-encoding"):
                continue
            fwd_headers[key] = value

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method=request.method,
                    url=target,
                    headers=fwd_headers,
                    content=body,
                )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="预览服务无法连接")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="预览服务响应超时")

        resp_headers = {}
        for key, value in resp.headers.items():
            low = key.lower()
            if low in ("transfer-encoding", "connection", "keep-alive"):
                continue
            resp_headers[key] = value

        return StreamingResponse(
            resp.aiter_bytes(),
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type", "application/octet-stream"),
        )

    # ── GET /live-edit/preview/{session_id} ──

    @router.get("/preview/{session_id}")
    async def get_session_preview(session_id: str):
        """Return the preview URL for a running session, if any."""
        session = session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")
        url = preview_manager.get_url(session_id)
        return {"url": url, "active": url is not None}

    # ── Admin: worktree management ──

    @router.get("/admin/worktrees")
    async def admin_worktrees(x_admin_key: str = Header("", alias="X-Admin-Key")):
        """List active live-edit worktrees with preview URLs, modified files,
        conflict detection, and system overview. Requires X-Admin-Key header."""
        if not admin_key or x_admin_key != admin_key:
            raise HTTPException(status_code=403, detail="需要有效的 admin key")
        try:
            import subprocess as _sp
            wts = vcs.list_worktrees()

            # Collect modified files per session for conflict detection
            files_by_session: dict[str, list[str]] = {}
            entries = []
            for wt in wts:
                sid = wt.get("session_id", "")
                active_session = session_store.get(sid)
                modified_files = []
                preview_url = ""
                entry = {
                    "session_id": sid,
                    "branch": wt.get("branch", ""),
                    "path": wt.get("path", ""),
                    "commit_hash": wt.get("commit_hash", ""),
                    "active": active_session is not None,
                    "preview_url": "",
                    "modified_files": [],
                    "conflicts": [],
                }
                if active_session:
                    modified_files = getattr(active_session, '_modified_files', []) or []
                    files_by_session[sid] = modified_files
                    entry["request"] = active_session.request[:100]
                    entry["mode"] = active_session._mode
                    entry["created_at"] = getattr(active_session, '_created_at', 0)
                    entry["modified_files"] = modified_files
                    preview_url = preview_manager.get_url(sid) or ""
                    if preview_url:
                        entry["preview_url"] = f"{config.preview.base_url}/live-edit/p/{sid}" if config.preview.base_url else preview_url
                else:
                    entry["request"] = ""
                    entry["mode"] = ""
                    entry["created_at"] = 0
                entries.append(entry)

            # ── Conflict detection: find files modified by more than one session ──
            file_to_sessions: dict[str, set[str]] = {}
            for sid, files in files_by_session.items():
                for f in files:
                    file_to_sessions.setdefault(f, set()).add(sid)
            conflicting_files = {f for f, sids in file_to_sessions.items() if len(sids) > 1}
            for entry in entries:
                sid = entry["session_id"]
                for f in entry["modified_files"]:
                    if f in conflicting_files:
                        others = [s for s in file_to_sessions.get(f, set()) if s != sid]
                        entry["conflicts"].extend(others)
                entry["conflicts"] = list(set(entry["conflicts"]))  # dedupe

            # ── System overview ──
            disk_mb = 0
            try:
                du = _sp.run(
                    ["du", "-sm", "/tmp/live-edit"],
                    capture_output=True, text=True, timeout=5,
                )
                disk_mb = int(du.stdout.strip().split()[0]) if du.stdout.strip() else 0
            except Exception:
                pass

            overview = {
                "active_sessions": session_store.count,
                "max_sessions": max_active,
                "preview_ports_used": sum(1 for e in entries if e["preview_url"]),
                "preview_port_start": config.preview.port_start,
                "preview_port_end": config.preview.port_end,
                "preview_enabled": config.preview.enabled,
                "worktree_disk_mb": disk_mb,
            }

            return {"overview": overview, "worktrees": entries}
        except Exception as e:
            logger.error("admin_worktrees error: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/admin/worktrees/{session_id}/cancel")
    async def admin_cancel_session(session_id: str, x_admin_key: str = Header("", alias="X-Admin-Key")):
        """Force-cancel an active session from admin. Requires X-Admin-Key header."""
        if not admin_key or x_admin_key != admin_key:
            raise HTTPException(status_code=403, detail="需要有效的 admin key")
        try:
            session = session_store.get(session_id)
            if session:
                session.cancel()
                return {"ok": True, "message": f"已取消会话: {session_id}"}
            else:
                return {"ok": False, "message": f"会话不存在或已过期: {session_id}"}
        except Exception as e:
            logger.error("admin_cancel error: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/admin/worktrees/{session_id}/cleanup")
    async def admin_cleanup_worktree(session_id: str, x_admin_key: str = Header("", alias="X-Admin-Key")):
        """Force-remove an orphaned live-edit worktree. Requires X-Admin-Key header."""
        if not admin_key or x_admin_key != admin_key:
            raise HTTPException(status_code=403, detail="需要有效的 admin key")
        try:
            # Try to remove from session store first
            session = session_store.get(session_id)
            if session:
                session_store.remove(session_id)
            # Find and remove the worktree
            wts = vcs.list_worktrees()
            for wt in wts:
                if wt.get("session_id") == session_id:
                    vcs.remove_worktree(wt["path"], session_id, force=True)
                    return {"ok": True, "message": f"已清理 worktree: {session_id}"}
            return {"ok": False, "message": f"未找到 worktree: {session_id}"}
        except Exception as e:
            logger.error("admin_cleanup error: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    return router
