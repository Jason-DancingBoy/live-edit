"""EditSession, agent loop, timeline compose, and error translation."""

import asyncio
import json
import logging
import os
import time
import traceback

from .tools import TOOLS, QA_TOOLS, _WRITE_TOOLS, execute_tool, get_mode_tools, _tool_summary, _summarize_thinking
from .provider import Provider
from .storage import Storage
from .vcs import VCS
from .config import Config, ModeConfig

logger = logging.getLogger("live-edit.engine")


# ── Error translation ──

_DEFAULT_ERROR_MAP = {
    "quick": {
        "old_string 在文件中未找到": "AI 发现文件内容已变化，会重新读取后调整",
        "old_string 匹配了": "AI 找到了多处匹配，会缩小范围重试",
        "文件不存在": "AI 想操作的文件不存在，请确认后再试",
        "路径越界": "操作已自动阻止（访问了项目外的文件）",
        "命令包含危险操作": "操作已自动阻止",
        "命令执行超时": "命令耗时过长，已自动终止",
        "write_file 只能覆写": "只能在该目录下创建或修改文件",
        "写入到项目外文件": "操作已自动阻止",
    },
    "deep": {},
    "qa": {},
}


def translate_error(error: str, mode: str = "quick", custom_map: dict | None = None,
                    config=None) -> str:
    """Translate technical errors to user-friendly messages per mode.

    Priority: custom_map > config.errors.<mode> > built-in defaults.
    """
    if custom_map is None and config is not None:
        try:
            custom_map = getattr(config.errors, mode, None) or getattr(config.errors, 'quick', {})
        except Exception:
            custom_map = {}
    error_map = custom_map or _DEFAULT_ERROR_MAP.get(mode, {})
    for key, friendly in error_map.items():
        if key in error:
            return friendly
    if mode == "quick":
        return f"操作执行时出现问题，AI 会自动重试：{error}"
    return error


def _repair_messages(messages: list[dict]) -> None:
    """Strip unpaired tool_use blocks from the tail of the conversation.

    Mutates messages in place. Needed when a previous run was cancelled or
    crashed after the assistant message was persisted but before all matching
    tool_result blocks were appended. The Anthropic API requires every
    tool_use to be immediately followed by a tool_result.
    """
    if not messages:
        return
    # Find the last assistant message
    last_ai = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_ai = i
            break
    if last_ai is None:
        return

    ai_msg = messages[last_ai]
    content = ai_msg.get("content")
    if not isinstance(content, list):
        return

    # Collect tool_use ids from this assistant message
    tu_ids = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tu_ids.append(block.get("id"))

    if not tu_ids:
        return

    # Check if the next message is a user message with matching tool_results
    if last_ai + 1 >= len(messages) or messages[last_ai + 1].get("role") != "user":
        next_content = []
    else:
        next_content = messages[last_ai + 1].get("content")
        if not isinstance(next_content, list):
            next_content = []

    matched = set()
    for block in next_content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            matched.add(block.get("tool_use_id"))

    unpaired = [tid for tid in tu_ids if tid not in matched]
    if not unpaired:
        return

    logger = logging.getLogger("live-edit.engine")
    logger.warning("Repairing messages: stripping %d unpaired tool_use(s): %s",
                   len(unpaired), unpaired)

    # Strip unpaired tool_use blocks
    cleaned = [b for b in content
               if not (isinstance(b, dict)
                       and b.get("type") == "tool_use"
                       and b.get("id") in unpaired)]
    if not cleaned:
        messages.pop(last_ai)
        # Also remove the orphaned user message if it only contained
        # tool_results that now have no matching tool_uses
        if last_ai < len(messages) and messages[last_ai].get("role") == "user":
            messages.pop(last_ai)
    else:
        messages[last_ai]["content"] = cleaned


# ── EditSession ──


class EditSession:
    """Manages one live-edit conversation."""

    def __init__(self, session_id: str, user_request: str):
        self.id = session_id
        self.request = user_request
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._approve_event = asyncio.Event()
        self._approve_result: dict | None = None
        self._done = False
        self._modified_files: list[str] = []
        self.messages: list[dict] = []
        self._committed = False
        self._commit_hash = ""
        self._mode = "quick"
        self._created_at = time.time()
        self._worktree_path: str = ""
        self._merged: bool = False
        self._cancelled = asyncio.Event()
        self._preview_url: str = ""

    def new_stream_queue(self):
        """Create a fresh queue for a new SSE connection (used for continuation)."""
        self.queue = asyncio.Queue()
        self._approve_event = asyncio.Event()
        self._approve_result = None

    async def wait_for_approval(self, tool_id: str, tool_data: dict,
                                 timeout: float = 300.0) -> dict:
        """Send tool_plan event and wait for frontend to call approve endpoint."""
        self._approve_event.clear()
        self._approve_result = None
        self.queue.put_nowait({"type": "tool_plan", "id": tool_id, **tool_data})
        try:
            await asyncio.wait_for(self._approve_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"approved": False, "reason": "用户超时未响应"}
        return self._approve_result or {"approved": False}

    def cancel(self):
        """Cancel this session. Unblocks any pending approval and signals the agent loop to stop."""
        self._cancelled.set()
        # Unblock any pending approval wait so the loop can exit immediately
        self._approve_result = {"approved": False, "reason": "用户取消了操作"}
        self._approve_event.set()
        self.emit("cancelled", message="会话已取消")

    def approve(self, tool_id: str, approved: bool):
        """Called by the approve endpoint to unblock the session."""
        self._approve_result = {"approved": approved}
        self._approve_event.set()

    def emit(self, event_type: str, **data):
        """Send an SSE event to the frontend."""
        self.queue.put_nowait({"type": event_type, **data})

    def cleanup(self, store: "SessionStore"):
        """Remove session from global registry."""
        store.remove(self.id)


# ── SessionStore ──


class SessionStore:
    """Global in-memory session registry with TTL and max capacity."""

    def __init__(self, max_active: int = 10, ttl_seconds: int = 1800):
        self._sessions: dict[str, EditSession] = {}
        self.max_active = max_active
        self.ttl_seconds = ttl_seconds

    def add(self, session: EditSession) -> bool:
        """Add a session. Returns False if at capacity."""
        self._expire_stale()
        if len(self._sessions) >= self.max_active:
            return False
        self._sessions[session.id] = session
        return True

    def get(self, session_id: str) -> EditSession | None:
        """Get a session by ID. Returns None if not found or expired."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.time() - session._created_at > self.ttl_seconds:
            self.remove(session_id)
            return None
        return session

    def remove(self, session_id: str):
        """Remove a session from the store."""
        self._sessions.pop(session_id, None)

    def _expire_stale(self):
        """Remove all expired sessions."""
        now = time.time()
        stale = [
            sid for sid, s in self._sessions.items()
            if now - s._created_at > self.ttl_seconds
        ]
        for sid in stale:
            self._sessions.pop(sid, None)

    @property
    def count(self) -> int:
        self._expire_stale()
        return len(self._sessions)


# ── Timeline ──


def build_timeline(vcs: VCS, storage: Storage, limit: int = 30) -> list[dict]:
    """Merge VCS live-edit commits with Storage abandoned sessions into a unified timeline."""
    entries = []

    commits = vcs.log_live_edit_commits(limit=limit)

    try:
        sessions = storage.get_sessions(limit=limit)
    except Exception:
        sessions = []

    sessions_by_hash: dict[str, dict] = {}
    for s in sessions:
        h = s.get("commit_hash", "")
        if h:
            sessions_by_hash[h] = s

    for c in commits:
        h = c.get("commit_hash", "")
        entries.append({
            "commit_hash": h,
            "message": c.get("message", ""),
            "date": c.get("date", ""),
            "is_live_edit": True,
            "session": sessions_by_hash.get(h),
        })

    committed_hashes = {c.get("commit_hash", "") for c in commits}
    for s in sessions:
        if s.get("commit_hash") not in committed_hashes or not s.get("committed"):
            entries.append({
                "commit_hash": s.get("commit_hash", ""),
                "message": s.get("request", ""),
                "date": s.get("created_at", ""),
                "is_live_edit": True,
                "session": s,
            })

    entries.sort(key=lambda e: e.get("date", ""), reverse=True)
    return entries[:limit]


# ── Agent loop ──


async def _build_system_prompt(config: Config, mode: str) -> str:
    """Build the full system prompt for a mode from config."""
    mode_cfg = config.modes.get(mode) if config.modes else None
    if not mode_cfg or not mode_cfg.prompt:
        return "You are a helpful AI assistant for code editing."

    prompt = mode_cfg.prompt
    parts = [getattr(prompt, 'base', '') or '',
             getattr(prompt, 'user_persona', '') or '',
             getattr(prompt, 'communication_rules', '') or '']
    extra = getattr(config.project, 'extra_context', '') if hasattr(config, 'project') else ''
    if extra:
        parts.append(extra)
    return "\n\n".join(p for p in parts if p)


async def _do_commit(session: EditSession, vcs: VCS, storage: Storage,
                     config=None):
    """Commit in worktree, merge to main branch, clean up worktree.

    If config.hooks.pre_commit is set, runs that command in the worktree
    before committing. A non-zero exit aborts the commit.
    """
    import subprocess as _subprocess
    try:
        msg = f"live-edit: {session.request[:80]}"

        # Step 0: run pre-commit hook if configured
        pre_commit_cmd = ""
        if config and hasattr(config, 'hooks') and config.hooks:
            pre_commit_cmd = getattr(config.hooks, 'pre_commit', '') or ""
        if pre_commit_cmd:
            logger.info("Session %s: running pre_commit hook: %s",
                        session.id, pre_commit_cmd)
            result = _subprocess.run(
                pre_commit_cmd, shell=True,
                capture_output=True, text=True, timeout=120,
                cwd=session._worktree_path,
            )
            if result.returncode != 0:
                output = (result.stdout + result.stderr)[:800]
                logger.warning(
                    "Session %s: pre_commit hook failed (exit %d): %s",
                    session.id, result.returncode, output)
                session.emit("error",
                    error=f"pre-commit 检查失败 (exit {result.returncode}):\n{output}")
                return

        # Step 1: commit inside the worktree
        wt_hash = vcs.commit_in_worktree(
            session._worktree_path, session._modified_files, msg)
        # Step 2: merge the worktree commit into the main branch
        merge_hash = vcs.merge_commit(wt_hash, msg)
        # Step 3: remove the worktree (branch merged, no longer needed)
        vcs.remove_worktree(session._worktree_path, session.id)
        session._merged = True

        session._commit_hash = merge_hash
        session._committed = True
        session.emit("done", committed=True, commit_hash=session._commit_hash,
                     message="更改已提交。刷新页面即可看到效果。", can_continue=True)
        logger.info("Session %s: committed %s", session.id, session._commit_hash)
    except RuntimeError as e:
        # Merge conflict
        logger.error("Merge conflict for session %s: %s", session.id, e)
        vcs.abort_merge()
        session.emit("error", error=f"合并冲突：{e}. 请手动解决或重新提交。")
    except Exception as e:
        logger.error("Commit error: %s", e)
        session.emit("error", error=f"提交失败: {e}")


async def run_edit_session(
    session: EditSession,
    provider: Provider,
    vcs: VCS,
    storage: Storage,
    config: Config,
    mode: str = "quick",
    continue_msg: str = "",
    preview_manager=None,
    session_store: SessionStore | None = None,
):
    """Run the agent loop for a session. Pushes SSE events to session.queue.

    Mode controls: system prompt, tool availability, approval behavior, error translation.
    """
    system_prompt = await _build_system_prompt(config, mode)
    tools = get_mode_tools(mode, config)
    session._mode = mode

    # ── Create isolated worktree for this session ──
    if not session._worktree_path:
        session._worktree_path = vcs.create_worktree(session.id)
        session._merged = False
    _root = session._worktree_path

    # ── Start preview server for this session ──
    if preview_manager:
        preview_url = await preview_manager.start(session.id, session._worktree_path)
        if preview_url:
            session._preview_url = preview_url
            session.emit("preview_ready", url=preview_url)

    if continue_msg and session.messages:
        messages = session.messages
        if messages and isinstance(messages[0].get("content"), str):
            if len(str(messages[0]["content"])) > 200:
                messages[0]["content"] = system_prompt
        # Repair any unpaired tool_use blocks left from a previous
        # crashed/cancelled session (safety net for old persisted data).
        _repair_messages(messages)
        messages.append({"role": "user", "content": continue_msg})
    else:
        messages = [
            {"role": "user", "content": system_prompt},
            {"role": "user", "content": session.request},
        ]

    # Let the AI know about the preview URL so it can tell the user
    if session._preview_url and not getattr(session, '_preview_announced', False):
        session._preview_announced = True
        messages.append({
            "role": "user",
            "content": f"预览服务器已启动: {session._preview_url}\n请在回复中告知用户可以通过此链接预览修改效果。",
        })

    max_rounds = config.timeouts.max_rounds if config and config.timeouts else 15
    round_num = 0
    _write_less_rounds = 0   # consecutive rounds without any write tool calls

    try:
        while round_num < max_rounds:
            if session._cancelled.is_set():
                logger.info("Session %s cancelled at round %d", session.id, round_num)
                break
            round_num += 1

            thinking_chunks = []
            text_chunks = []
            _thinking_started = []

            def on_thinking(t):
                if not _thinking_started:
                    _thinking_started.append(True)
                    session.emit("thinking_started")
                thinking_chunks.append(t)

            content_blocks = await provider.call_with_tools(
                messages=messages,
                tools=tools,
                on_thinking=on_thinking,
                on_text=lambda t: text_chunks.append(t) or session.emit("text", text=t),
            )

            if content_blocks is None:
                session.emit("error", error="LLM 调用失败")
                break

            # Check cancellation after potentially long LLM call
            if session._cancelled.is_set():
                logger.info("Session %s cancelled after LLM call", session.id)
                break

            # Emit summarized thinking if any
            if thinking_chunks:
                full = "".join(thinking_chunks).strip()
                if full:
                    session.emit("thinking", text=_summarize_thinking(full))

            # Separate text and tool_use blocks
            text_parts = []
            tool_uses = []
            for block in content_blocks:
                if block is None:
                    continue
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_uses.append(block)

            # Build assistant message for conversation history
            assistant_content = []
            for block in content_blocks:
                if block is None:
                    continue
                if block.get("type") == "text":
                    assistant_content.append({"type": "text", "text": block.get("text", "")})
                elif block.get("type") == "thinking":
                    assistant_content.append({"type": "thinking", "thinking": block.get("thinking", "")})
                elif block.get("type") == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": block.get("input", {}),
                    })

            if not tool_uses:
                messages.append({"role": "assistant", "content": assistant_content})
                if not session._modified_files and round_num < max_rounds - 1 and mode != "qa":
                    # In deep mode, a pure-text response with no prior edits means the model
                    # got stuck in analysis — nudge it toward concrete action.
                    if mode == "deep":
                        _write_less_rounds += 1
                        if _write_less_rounds <= 3:
                            messages.append({"role": "user", "content": "分析已经足够了。现在请停止搜索和阅读，直接使用 edit_file（或 write_file）工具执行代码修改。不要再返回纯文本分析。"})
                            continue
                    elif round_num <= 3:
                        total_text = "".join(text_parts).strip()
                        if len(total_text) < 200:
                            messages.append({"role": "user", "content": "请继续，进行实际的代码修改（不要只描述计划）。"})
                            continue
                break

            # Execute tools BEFORE appending the assistant message. This keeps
            # the conversation history consistent even if tool execution fails
            # mid-way or the user rejects a tool — no tool_use blocks are ever
            # persisted without matching tool_result blocks.
            tool_results = []
            all_approved = True
            _round_has_write = False

            for i, tool in enumerate(tool_uses):
                tool_name = tool["name"]
                tool_id = tool["id"]
                tool_input = tool.get("input", {})

                needs_approval = (
                    mode == "quick"
                    and tool_name in _WRITE_TOOLS
                )

                if needs_approval:
                    reason = tool_input.get("reason", "")
                    summary = _tool_summary(tool_name, tool_input)
                    result = await session.wait_for_approval(tool_id, {
                        "tool": tool_name,
                        "args": tool_input,
                        "reason": reason,
                        "summary": summary,
                    })
                    if not result.get("approved"):
                        all_approved = False
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": [{"type": "text", "text": "用户拒绝执行此操作。"}],
                        })
                        session.emit("tool_result", id=tool_id, ok=False, error="用户拒绝执行")
                        # Fill tool_results for remaining unprocessed tool_uses,
                        # otherwise the API rejects the next request with a 400
                        # ("tool_use ids without tool_result blocks").
                        for remaining in tool_uses[i + 1:]:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": remaining["id"],
                                "content": [{"type": "text", "text": "操作已跳过（前一个操作被用户拒绝）。"}],
                            })
                            session.emit("tool_result", id=remaining["id"], ok=False, error="已跳过")
                        break
                elif mode == "deep":
                    session.emit("tool_plan",
                        id=tool_id, tool=tool_name, args=tool_input,
                        reason=tool_input.get("reason", ""),
                        summary=_tool_summary(tool_name, tool_input),
                        auto=True,
                    )

                exec_result = await execute_tool(tool_name, tool_input, _root, config)
                if not exec_result.get("ok") and mode == "quick":
                    exec_result["error"] = translate_error(exec_result.get("error", ""), "quick", config=config)
                session.emit("tool_result", id=tool_id, **exec_result)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": [{"type": "text", "text": json.dumps(exec_result, ensure_ascii=False)}],
                })

                if tool_name in ("edit_file", "write_file"):
                    _round_has_write = True
                    if exec_result.get("ok"):
                        if tool_input.get("path") not in session._modified_files:
                            session._modified_files.append(tool_input["path"])

            # Now append assistant + tool_results atomically — every tool_use
            # has a matching tool_result at this point.
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})

            # Track consecutive write-less rounds; reset when a write tool was used
            if _round_has_write:
                _write_less_rounds = 0
            else:
                _write_less_rounds += 1

            # In deep mode, after 3+ consecutive read-only rounds with no edits, nudge
            if (mode == "deep" and _write_less_rounds >= 3
                    and not session._modified_files and round_num < max_rounds - 1):
                messages.append({"role": "user", "content": "你已经做了充分的调研。现在必须立即执行代码修改。请使用 edit_file 工具直接修改文件，不要再使用 search_code、read_file 或任何只读工具。如果你不确定 old_string 的精确内容，先用 read_file 读取关键行再立即 edit_file。"})
                _write_less_rounds = 0   # reset to avoid repeated nudges

        # After the loop: detect all changes in the worktree (including new files)
        import subprocess as _sp
        # Stage everything so git diff --cached captures new + modified files
        _sp.run(
            ["git", "-C", _root, "add", "-A"],
            capture_output=True, text=True, timeout=10,
        )
        all_modified = set(session._modified_files)
        try:
            git_files = _sp.run(
                ["git", "-C", _root, "diff", "--cached", "--name-only"],
                capture_output=True, text=True, timeout=10,
            )
            for f in git_files.stdout.strip().split("\n"):
                f = f.strip()
                if f:
                    all_modified.add(f)
        except Exception:
            pass
        session._modified_files = sorted(all_modified)

        if not session._modified_files:
            _sp.run(
                ["git", "-C", _root, "reset", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            session.emit("done", committed=False, message="完成", can_continue=True)
        else:
            try:
                # Get diffs from staged changes in the worktree
                diff_stat_result = _sp.run(
                    ["git", "-C", _root, "diff", "--cached", "--stat", "--"] + session._modified_files,
                    capture_output=True, text=True, timeout=10,
                )
                diff_stat = diff_stat_result.stdout.strip() or "(无变更)"

                diff_full_result = _sp.run(
                    ["git", "-C", _root, "diff", "--cached", "--"] + session._modified_files,
                    capture_output=True, text=True, timeout=10,
                )
                diff_full = diff_full_result.stdout.strip()

                if not diff_full:
                    _sp.run(
                        ["git", "-C", _root, "reset", "HEAD"],
                        capture_output=True, text=True, timeout=10,
                    )
                    session.emit("done", committed=False, message="没有需要提交的变更", can_continue=True)
                    _persist_session(session, storage, messages)
                    return

                session.emit("diff", files=session._modified_files,
                             summary=diff_stat, diff=diff_full)

            except Exception as e:
                logger.error("Diff error: %s", e)
                session.emit("error", error=f"生成 diff 失败: {e}")
                return

            if mode == "deep":
                await _do_commit(session, vcs, storage, config)
            else:
                final = await session.wait_for_approval("__final__", {
                    "tool": "final_commit",
                    "files": session._modified_files,
                    "summary": diff_stat,
                }, timeout=600.0)

                if final.get("approved"):
                    await _do_commit(session, vcs, storage, config)
                else:
                    # Rollback: just remove the worktree, no merge
                    try:
                        vcs.remove_worktree(session._worktree_path, session.id, force=True)
                        session._merged = True
                    except Exception:
                        pass
                    session._committed = False
                    session.emit("done", committed=False, message="更改已放弃。", can_continue=True)

    except Exception as e:
        logger.error("Session %s error: %s\n%s", session.id, e, traceback.format_exc())
        session.emit("error", error=str(e))

    finally:
        session._done = True
        session.messages = messages
        _persist_session(session, storage, messages)
        # Stop preview server before cleaning up worktree
        if preview_manager:
            await preview_manager.stop(session.id)
        # Clean up worktree if not merged/removed yet (e.g. exception before commit)
        if not session._merged and session._worktree_path:
            try:
                vcs.remove_worktree(session._worktree_path, session.id, force=True)
                session._merged = True
            except Exception:
                pass
        session.queue.put_nowait(None)


def _persist_session(session: EditSession, storage: Storage, messages: list[dict]):
    """Save session to persistent storage."""
    try:
        msgs_to_save = []
        for m in messages:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                if len(m["content"]) > 500 and "项目技术栈" in m["content"]:
                    continue
            msgs_to_save.append(m)
        msgs_json = json.dumps(msgs_to_save, ensure_ascii=False)
        storage.save_session(
            session_id=session.id,
            request=session.request,
            committed=session._committed,
            files=session._modified_files,
            commit_hash=session._commit_hash,
            messages_json=msgs_json,
            mode=session._mode,
        )
    except Exception as e:
        logger.warning("Failed to persist session %s: %s", session.id, e)


async def continue_edit_session(
    session: EditSession,
    new_request: str,
    provider: Provider,
    vcs: VCS,
    storage: Storage,
    config: Config,
    mode: str = "",
    preview_manager=None,
    session_store: SessionStore | None = None,
):
    """Continue an existing session with a new request."""
    session.request = new_request
    session._done = False
    effective_mode = mode or session._mode
    if mode:
        session._mode = mode

    # If worktree was cleaned up (session completed), reset so a new one is created
    if session._merged or not session._worktree_path:
        session._worktree_path = ""
        session._merged = False
    elif not os.path.isdir(session._worktree_path):
        # Worktree was removed externally
        session._worktree_path = ""
        session._merged = False

    await run_edit_session(
        session=session,
        provider=provider,
        vcs=vcs,
        storage=storage,
        config=config,
        mode=effective_mode,
        continue_msg=new_request,
        preview_manager=preview_manager,
        session_store=session_store,
    )
