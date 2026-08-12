"""FastAPI router for live-edit endpoints."""

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from .audit import AuditLog, NullAuditLog, SQLiteAuditLog
from .config import parse_config
from .engine import (
    EditSession,
    SessionStore,
    build_timeline,
    continue_edit_session,
    rehydrate_session,
    run_edit_session,
)
from .logging import configure_logging, set_correlation_id, set_session_id
from .metrics import Metrics
from .preview import PreviewManager
from .provider import AnthropicCompatibleProvider, Provider
from .storage import SQLiteStorage, Storage
from .vcs import VCS, GitVCS

logger = logging.getLogger("live-edit.router")


class StreamRequest(BaseModel):
    request: str
    mode: str = "quick"
    base_session_id: str = ""


class ContinueRequest(BaseModel):
    request: str
    mode: str = "quick"


class ApproveRequest(BaseModel):
    approved: bool = True


class BatchApproveRequest(BaseModel):
    enabled: bool = True


class KnowledgeUpload(BaseModel):
    source_path: str
    content: str
    metadata: str = "{}"


def _resolve_base_ref(storage, base_session_id: str) -> str:
    """Return the base commit hash for a session fork, or raise ValueError."""
    if not base_session_id:
        return ""
    base_sess = storage.get_session_detail(base_session_id)
    if not base_sess:
        raise ValueError("无效的基会话")
    commit: str = base_sess.get("commit_hash", "")
    if not base_sess.get("committed") or not commit:
        raise ValueError("基会话尚未合并，无法作为基础")
    return commit


def _resolve_api_key(config) -> str:
    """Resolve API key from environment variable named in config."""
    env_var = getattr(config.llm, "api_key_env", "") if hasattr(config, "llm") else ""
    return os.environ.get(env_var, "")


def setup_live_edit(
    project_root: str = ".",
    config_path: str = ".live-edit.toml",
    provider: Provider | None = None,
    storage: Storage | None = None,
    vcs: VCS | None = None,
    api_key: str = "",
    admin_key: str = "",
    tool_registry: object | None = None,
    audit_log: AuditLog | None = None,
    metrics: Metrics | None = None,
    session_store: SessionStore | None = None,
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

    # Correlation middleware: echo X-Request-ID and scope the correlation contextvar to
    # every request (including SSE streams). APIRouter exposes no middleware API in the
    # installed FastAPI/Starlette, so we wrap each route handler via a custom route class.
    class CorrelationRoute(APIRoute):
        def get_route_handler(self):
            original_handler = super().get_route_handler()

            async def handler(request: Request):
                cid = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:12]}"
                set_correlation_id(cid)
                response = await original_handler(request)
                response.headers["X-Request-ID"] = cid
                return response

            return handler

    router = APIRouter(prefix="/live-edit", tags=["live-edit"], route_class=CorrelationRoute)

    # Load config
    resolved_config_path = (
        os.path.join(project_root, config_path) if not os.path.isabs(config_path) else config_path
    )
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
        vcs = GitVCS(project_root, worktree_ttl=config.timeouts.stale_worktree_ttl)

    # Observability: audit log + metrics + structured logging.
    if audit_log is None and config.observability.audit_enabled:
        audit_log = SQLiteAuditLog(os.path.join(project_root, "live_edit.db"))
    if metrics is None:
        metrics = Metrics()
    configure_logging(
        level=config.observability.log_level, json_logs=config.observability.json_logs
    )
    if audit_log is None:
        # audit_enabled=false (or a caller that injected nothing) must never break
        # the app: audit writes are best-effort no-ops, not a crash.
        audit_log = NullAuditLog()

    # Global session store
    ttl = getattr(config.timeouts, "session_ttl", 1800) if hasattr(config, "timeouts") else 1800
    max_active = getattr(config.sessions, "max_active", 10) if hasattr(config, "sessions") else 10
    if session_store is None:
        session_store = SessionStore(max_active=max_active, ttl_seconds=ttl, audit_log=audit_log)

    # Tool registry
    if tool_registry is None:
        from .tool_registry import DefaultToolRegistry, set_global_registry

        tool_registry = DefaultToolRegistry()
        tool_registry.load_builtin_tools()
        tool_registry.load_toml_tools(config)
        # Global registry must be set before plugin modules are imported: the
        # @tool decorator registers into _global_registry at module import time.
        set_global_registry(tool_registry)
        plugin_dir = os.path.join(project_root, "live_edit_tools")
        tool_registry.load_plugin_tools(plugin_dir)

    from .tools import _set_registry

    _set_registry(tool_registry)

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

        if req.base_session_id:
            try:
                session.base_ref = _resolve_base_ref(storage, req.base_session_id)
            except ValueError as e:
                audit_log.record(
                    "session_rejected",
                    target=session_id,
                    session_id=session_id,
                    result="blocked",
                    detail={"reason": "invalid_base", "base_session_id": req.base_session_id},
                )
                raise HTTPException(status_code=400, detail=str(e)) from e
            session.base_session_id = req.base_session_id

        if not session_store.add(session):
            audit_log.record(
                "session_rejected",
                target=session_id,
                session_id=session_id,
                result="blocked",
                detail={"reason": "max_active_reached"},
            )
            metrics.inc("live_edit_sessions_total", {"outcome": "rejected"})
            raise HTTPException(status_code=503, detail="会话数已达上限，请稍后再试")

        mode = req.mode or getattr(config.ui, "default_mode", "quick")

        audit_log.record(
            "session_start",
            target=session_id,
            session_id=session_id,
            detail={"mode": mode, "base_session_id": req.base_session_id or ""},
        )
        if req.base_session_id:
            audit_log.record(
                "session_fork",
                target=session_id,
                session_id=session_id,
                detail={"base_session_id": req.base_session_id, "base_commit": session.base_ref},
            )
        metrics.inc("live_edit_sessions_total", {"outcome": "started"})
        set_session_id(session_id)

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
                    tool_registry=tool_registry,
                    audit_log=audit_log,
                    metrics=metrics,
                )
            )

            # Surface a crashed task promptly: the engine's finally that emits a
            # trailing None never runs when create_worktree raises (it's outside
            # run_edit_session's try), so the queue loop would otherwise block on
            # the 180s timeout. The done-callback converts a crash into queue
            # events that flow through the normal yield path immediately.
            def _surface_task_error(t):
                try:
                    exc = t.exception()
                except asyncio.CancelledError:
                    return
                if exc is not None:
                    session.queue.put_nowait({"type": "error", "error": f"会话执行出错: {exc}"})
                    session.queue.put_nowait(None)

            task.add_done_callback(_surface_task_error)

            try:
                while True:
                    try:
                        event = await asyncio.wait_for(session.queue.get(), timeout=180.0)
                    except asyncio.TimeoutError:
                        audit_log.record(
                            "session_timeout",
                            target=session_id,
                            session_id=session_id,
                            result="timeout",
                        )
                        yield f"data: {json.dumps({'type': 'error', 'error': '会话超时'})}\n\n"
                        break

                    if event is None:
                        break

                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                # If client disconnects, cancel the backend task
                if not session._done:
                    session.cancel()
                    audit_log.record(
                        "session_disconnect",
                        target=session_id,
                        session_id=session_id,
                        result="cancelled",
                    )
                if not task.done():
                    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=30.0)

            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # error already surfaced via _surface_task_error

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
            # Crash recovery: rebuild the session from its persisted record.
            detail = storage.get_session_detail(session_id)
            session = rehydrate_session(session_id, detail) if detail else None
            if session is None:
                raise HTTPException(status_code=404, detail="会话不存在或已过期")
            audit_log.record(
                "session_recovered",
                target=session_id,
                session_id=session_id,
                result="recovered",
            )
            if not session_store.add(session):
                raise HTTPException(status_code=503, detail="会话数已达上限，请稍后再试")

        session.new_stream_queue()
        mode = req.mode or session._mode
        audit_log.record(
            "session_continue",
            target=session_id,
            session_id=session_id,
            detail={"mode": mode},
        )
        set_session_id(session_id)

        async def event_generator() -> AsyncIterator[str]:
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
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
                    tool_registry=tool_registry,
                    audit_log=audit_log,
                    metrics=metrics,
                )
            )

            # Surface a crashed task promptly (see start_stream for rationale).
            def _surface_task_error(t):
                try:
                    exc = t.exception()
                except asyncio.CancelledError:
                    return
                if exc is not None:
                    session.queue.put_nowait({"type": "error", "error": f"会话执行出错: {exc}"})
                    session.queue.put_nowait(None)

            task.add_done_callback(_surface_task_error)

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
                    audit_log.record(
                        "session_disconnect",
                        target=session_id,
                        session_id=session_id,
                        result="cancelled",
                    )
                if not task.done():
                    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=30.0)

            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # error already surfaced via _surface_task_error

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── POST /live-edit/approve/{session_id}/batch ──
    # NOTE: registered BEFORE /approve/{session_id}/{tool_id} because Starlette
    # matches routes in registration order — "/approve/{session_id}/batch" would
    # otherwise be captured by the generic {tool_id} route.

    @router.post("/approve/{session_id}/batch")
    async def batch_approve(session_id: str, req: BatchApproveRequest):
        """Enable/disable auto-approval for all subsequent write tools in a session."""
        session = session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")
        session.set_auto_approve(req.enabled)
        audit_log.record(
            "approve_batch",
            target=session_id,
            session_id=session_id,
            result="enabled" if req.enabled else "disabled",
        )
        return {"ok": True, "enabled": req.enabled}

    # ── POST /live-edit/approve/{session_id}/{tool_id} ──

    @router.post("/approve/{session_id}/{tool_id}")
    async def approve_tool(session_id: str, tool_id: str, req: ApproveRequest):
        """Approve or reject a tool execution (for quick mode)."""
        session = session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")
        session.approve(tool_id, req.approved)
        decision = "approved" if req.approved else "rejected"
        audit_log.record(
            "approve" if req.approved else "reject",
            target=tool_id,
            session_id=session_id,
            result=decision,
        )
        metrics.inc("live_edit_approvals_total", {"decision": decision})
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
        audit_log.record("cancel", target=session_id, session_id=session_id, result="cancelled")
        return {"ok": True}

    # ── GET /live-edit/timeline ──

    @router.get("/timeline")
    async def get_timeline(
        limit: int = Query(default=30, le=100), diff_for: str = Query(default="")
    ):
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
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=project_root,
                )
                root_hash = r.stdout.strip()[:8]
                info = subprocess.run(
                    ["git", "log", "-1", "--format=%s|%ai", root_hash],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=project_root,
                )
                parts = info.stdout.strip().split("|", 1)
                entries.insert(
                    0,
                    {
                        "commit_hash": root_hash,
                        "message": parts[0] if parts else "Initial commit",
                        "date": parts[1] if len(parts) > 1 else "",
                        "is_initial": True,
                        "is_live_edit": False,
                        "session": None,
                    },
                )
            except Exception:
                pass

            return {"entries": entries}
        except Exception as e:
            logger.error("Timeline error: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    # ── GET /live-edit/history
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
        evidence_json = storage.get_evidence(session_id) if storage else None
        evidence = None
        if evidence_json and isinstance(evidence_json, str):
            try:
                evidence = json.loads(evidence_json)
            except Exception:  # noqa: BLE001 — 损坏/非 JSON 证据视为无证据，不 500
                evidence = None
        detail["evidence"] = evidence
        return detail

    # ── POST /live-edit/revert/{commit_hash}/preview ──

    @router.post("/revert/{commit_hash}/preview")
    async def revert_preview(commit_hash: str):
        """Dry-run revert to check for conflicts."""
        preview = vcs.revert_preview(commit_hash)
        if not preview.ok:
            result = "error"
        elif preview.conflicts or not preview.can_revert:
            result = "conflict"
        else:
            result = "ok"
        audit_log.record(
            "revert_preview",
            target=commit_hash,
            session_id="",
            result=result,
            detail={"message": preview.error or ""},
        )
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
        audit_log.record(
            "revert_execute",
            target=commit_hash,
            session_id="",
            result="ok" if result.ok else "error",
            detail={"message": getattr(result, "message", "")},
        )
        metrics.inc("live_edit_reverts_total", {"outcome": "ok" if result.ok else "error"})
        if result.ok and hasattr(config.hooks, "post_revert") and config.hooks.post_revert:
            import subprocess

            try:
                subprocess.run(
                    config.hooks.post_revert,
                    shell=True,
                    capture_output=True,
                    timeout=30,
                    cwd=project_root,
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

    # ── GET /live-edit/metrics ──

    @router.get("/metrics")
    async def metrics_endpoint():
        """Prometheus text metrics. Gate at the reverse proxy for production."""
        return Response(content=metrics.render(), media_type="text/plain")

    # ── GET /live-edit/admin/audit ──

    @router.get("/admin/audit")
    async def admin_audit(
        action: str = Query(default=""),
        actor: str = Query(default=""),
        session_id: str = Query(default=""),
        limit: int = Query(default=100, le=1000),
        after: str = Query(default=""),
        before: str = Query(default=""),
        x_admin_key: str = Header("", alias="X-Admin-Key"),
    ):
        """Query the append-only audit trail. Requires X-Admin-Key."""
        if not admin_key or x_admin_key != admin_key:
            audit_log.record(
                "failed_admin_auth", actor="unknown", target="admin_audit", result="blocked"
            )
            raise HTTPException(status_code=403, detail="需要有效的 admin key")
        events = audit_log.query(
            action=action or None,
            actor=actor or None,
            session_id=session_id or None,
            limit=limit,
            after=after or None,
            before=before or None,
        )
        return {"events": [e.to_dict() for e in events]}

    # ── Knowledge base endpoints ──

    @router.post("/knowledge")
    async def upload_knowledge(request: Request, body: KnowledgeUpload) -> dict:
        """Upload a document snippet to the knowledge base."""
        try:
            meta = json.loads(body.metadata)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail="metadata must be valid JSON") from e

        if not body.source_path.startswith("api:"):
            raise HTTPException(
                status_code=400,
                detail="source_path must start with 'api:' for API-uploaded documents",
            )

        mem_mgr = getattr(request.app.state, "memory_manager", None)
        if mem_mgr is None:
            raise HTTPException(status_code=503, detail="Memory system not available")

        try:
            mem_mgr.add_knowledge(body.source_path, body.content, meta)
        except Exception as e:
            audit_log.record(
                "knowledge_upload",
                target=body.source_path,
                result="error",
                detail={"message": str(e)},
            )
            raise HTTPException(status_code=500, detail=str(e)) from e

        audit_log.record("knowledge_upload", target=body.source_path, result="ok")
        return {"ok": True, "source_path": body.source_path}

    @router.delete("/knowledge/{source_path:path}")
    async def delete_knowledge(request: Request, source_path: str) -> dict:
        """Delete an API-uploaded knowledge document."""
        mem_mgr = getattr(request.app.state, "memory_manager", None)
        if mem_mgr is None:
            raise HTTPException(status_code=503, detail="Memory system not available")

        try:
            mem_mgr.delete_knowledge(source_path)
        except ValueError as e:
            audit_log.record(
                "knowledge_delete",
                target=source_path,
                result="error",
                detail={"message": str(e)},
            )
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            audit_log.record(
                "knowledge_delete",
                target=source_path,
                result="error",
                detail={"message": str(e)},
            )
            raise HTTPException(status_code=500, detail=str(e)) from e

        audit_log.record("knowledge_delete", target=source_path, result="ok")
        return {"ok": True}

    @router.get("/knowledge")
    async def list_knowledge(request: Request) -> dict:
        """List all knowledge base documents."""
        mem_mgr = getattr(request.app.state, "memory_manager", None)
        if mem_mgr is None:
            return {"documents": []}
        return {"documents": mem_mgr.list_knowledge()}

    # ── Preview reverse proxy (routes to session's uvicorn on 127.0.0.1) ──

    @router.api_route(
        "/p/{session_id}/{rest:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )
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
            raise HTTPException(status_code=502, detail="预览服务无法连接") from None
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="预览服务响应超时") from None

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

    @router.api_route(
        "/p/{session_id}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]
    )
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
            raise HTTPException(status_code=502, detail="预览服务无法连接") from None
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="预览服务响应超时") from None

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
            audit_log.record(
                "failed_admin_auth", actor="unknown", target="admin_worktrees", result="blocked"
            )
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
                modified_files: list[str] = []
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
                    modified_files = getattr(active_session, "_modified_files", []) or []
                    files_by_session[sid] = modified_files
                    entry["request"] = active_session.request[:100]
                    entry["mode"] = active_session._mode
                    entry["created_at"] = getattr(active_session, "_created_at", 0)
                    entry["modified_files"] = modified_files
                    preview_url = preview_manager.get_url(sid) or ""
                    if preview_url:
                        entry["preview_url"] = (
                            f"{config.preview.base_url}/live-edit/p/{sid}"
                            if config.preview.base_url
                            else preview_url
                        )
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
                    capture_output=True,
                    text=True,
                    timeout=5,
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
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/admin/worktrees/{session_id}/cancel")
    async def admin_cancel_session(
        session_id: str,
        x_admin_key: str = Header("", alias="X-Admin-Key"),
    ):
        """Force-cancel an active session from admin. Requires X-Admin-Key header."""
        if not admin_key or x_admin_key != admin_key:
            audit_log.record(
                "failed_admin_auth", actor="unknown", target="admin_cancel", result="blocked"
            )
            raise HTTPException(status_code=403, detail="需要有效的 admin key")
        try:
            session = session_store.get(session_id)
            if session:
                session.cancel()
                audit_log.record("admin_cancel", target=session_id, result="ok")
                return {"ok": True, "message": f"已取消会话: {session_id}"}
            else:
                return {"ok": False, "message": f"会话不存在或已过期: {session_id}"}
        except Exception as e:
            logger.error("admin_cancel error: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/admin/worktrees/{session_id}/cleanup")
    async def admin_cleanup_worktree(
        session_id: str,
        x_admin_key: str = Header("", alias="X-Admin-Key"),
    ):
        """Force-remove an orphaned live-edit worktree. Requires X-Admin-Key header."""
        if not admin_key or x_admin_key != admin_key:
            audit_log.record(
                "failed_admin_auth", actor="unknown", target="admin_cleanup", result="blocked"
            )
            raise HTTPException(status_code=403, detail="需要有效的 admin key")
        try:
            # Try to remove from session store first
            session = session_store.get(session_id)
            if session:
                session_store.remove(session_id)
            await preview_manager.stop(session_id)
            # Find and remove the worktree
            wts = vcs.list_worktrees()
            for wt in wts:
                if wt.get("session_id") == session_id:
                    vcs.discard_session_branch(session_id, worktree_path=wt["path"])
                    audit_log.record("admin_cleanup", target=session_id, result="ok")
                    return {"ok": True, "message": f"已清理 worktree: {session_id}"}
            return {"ok": False, "message": f"未找到 worktree: {session_id}"}
        except Exception as e:
            logger.error("admin_cleanup error: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/admin/branches")
    async def admin_list_unmerged_branches(x_admin_key: str = Header("", alias="X-Admin-Key")):
        """List live-edit branches not yet merged into main. Requires X-Admin-Key."""
        if not admin_key or x_admin_key != admin_key:
            audit_log.record(
                "failed_admin_auth", actor="unknown", target="admin_list_branches", result="blocked"
            )
            raise HTTPException(status_code=403, detail="需要有效的 admin key")
        try:
            raw = vcs.list_unmerged_branches()
            branches = []
            for r in raw:
                sid = r.get("session_id", "")
                summary = ""
                detail = storage.get_session_detail(sid) if sid else None
                if detail:
                    summary = (detail.get("request") or "")[:200]
                r["summary"] = summary
                branches.append(r)
            return {"branches": branches}
        except Exception as e:
            logger.error("admin_list_branches error: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    class MergeRequest(BaseModel):
        reason: str = ""

    @router.post("/admin/branches/{session_id}/merge")
    async def admin_merge_branch(
        session_id: str,
        x_admin_key: str = Header("", alias="X-Admin-Key"),
        req: MergeRequest | None = None,
    ):
        """Merge live-edit/<session_id> into main. Requires X-Admin-Key.

        On conflict: aborts the merge, returns 409 with conflict=true,
        and keeps the branch for manual resolution.
        """
        if not admin_key or x_admin_key != admin_key:
            audit_log.record(
                "failed_admin_auth", actor="unknown", target="admin_merge", result="blocked"
            )
            raise HTTPException(status_code=403, detail="需要有效的 admin key")
        branch = f"live-edit/{session_id}"
        # Verify-then-approve gate: read stored evidence; BLOCK without a
        # reason override stops the merge.
        evidence_json = storage.get_evidence(session_id) if storage else None
        decision = None
        if evidence_json and isinstance(evidence_json, str):
            from .verify.evidence import Evidence

            try:
                decision = Evidence.from_dict(json.loads(evidence_json)).decision
            except Exception:
                # 损坏/非 JSON 证据视为无证据，走正常合并路径，不让合并端 500。
                decision = None
        if decision == "block" and not (req and req.reason and req.reason.strip()):
            # 纯空白 reason 不构成强制放行理由；记审计后拒绝，不让合并端静默成功。
            audit_log.record(
                "admin_merge_blocked",
                target=session_id,
                result="blocked",
                detail={"reason": (req.reason if req else "") or ""},
            )
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "该改动被验证阻断，需提供 reason 强制放行",
                    "blocked": True,
                },
            )
        try:
            # Verify branch exists
            import subprocess as _sp

            repo_cwd = vcs.repo_path if hasattr(vcs, "repo_path") else None
            check = _sp.run(
                ["git", "rev-parse", "--verify", branch],
                capture_output=True,
                cwd=repo_cwd,
            )
            if check.returncode != 0:
                raise HTTPException(status_code=404, detail=f"分支不存在: {branch}")

            # Resolve branch tip
            tip = _sp.run(
                ["git", "rev-parse", branch],
                capture_output=True,
                text=True,
                cwd=repo_cwd,
            ).stdout.strip()

            msg = f"live-edit: merge {branch}"
            merge_hash = vcs.merge_commit(tip, msg)
            if decision == "block":
                audit_log.record(
                    "admin_merge_override",
                    target=session_id,
                    result="ok",
                    detail={"reason": (req.reason if req else "") or ""},
                )
                merge_result = "override"
            else:
                merge_result = "auto_approve" if decision == "auto_approve" else "ok"
            audit_log.record("admin_merge", target=session_id, result=merge_result)
            await preview_manager.stop(session_id)
            # Branch merged — safe to delete
            try:
                vcs.discard_session_branch(session_id)
            except Exception as _e:
                logger.warning("post-merge branch delete failed for %s: %s", session_id, _e)
            return {"ok": True, "commit_hash": merge_hash, "decision": decision}
        except HTTPException:
            raise
        except RuntimeError as e:
            # Merge conflict
            with contextlib.suppress(Exception):
                vcs.abort_merge()
            audit_log.record(
                "admin_merge", target=session_id, result="conflict", detail={"message": str(e)}
            )
            logger.warning("merge conflict for %s: %s", session_id, e)
            return JSONResponse(
                status_code=409,
                content={"detail": f"合并冲突，请手动解决: {e}", "conflict": True},
            )
        except Exception as e:
            logger.error("admin_merge error: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/admin/branches/{session_id}/delete")
    async def admin_delete_branch(
        session_id: str, x_admin_key: str = Header("", alias="X-Admin-Key")
    ):
        """Delete live-edit/<session_id> branch and any leftover worktree.
        Requires X-Admin-Key."""
        if not admin_key or x_admin_key != admin_key:
            audit_log.record(
                "failed_admin_auth", actor="unknown", target="admin_delete", result="blocked"
            )
            raise HTTPException(status_code=403, detail="需要有效的 admin key")
        try:
            await preview_manager.stop(session_id)
            vcs.discard_session_branch(session_id)
            # Best-effort: remove session from storage
            try:
                if hasattr(storage, "remove"):
                    storage.remove(session_id)
            except Exception as _e:
                logger.warning("storage remove for %s failed: %s", session_id, _e)
            audit_log.record("admin_delete", target=session_id, result="ok")
            return {"ok": True}
        except Exception as e:
            logger.error("admin_delete_branch error: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    return router
