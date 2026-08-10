"""Tests for live_edit.engine — EditSession, agent loop, timeline, error translation."""

import asyncio
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from live_edit.config import (
    Config,
    ErrorTranslations,
    EvaluationConfig,
    HooksConfig,
    LLMConfig,
    ModeConfig,
    ModePromptConfig,
    ProjectConfig,
    SafetyConfig,
    SessionsConfig,
    TimeoutsConfig,
    UIConfig,
)
from live_edit.engine import (
    EditSession,
    SessionStore,
    build_timeline,
    continue_edit_session,
    run_edit_session,
    translate_error,
)

# ── translate_error ──


class TestTranslateError:
    def test_quick_mode_translates_technical_error(self):
        result = translate_error("old_string 在文件中未找到", "quick")
        assert "文件内容已变化" in result or "old_string" not in result

    def test_deep_mode_passes_through_raw_error(self):
        result = translate_error("old_string 在文件中未找到", "deep")
        assert "old_string 在文件中未找到" in result

    def test_qa_mode_passes_through_raw_error(self):
        result = translate_error("路径越界: ../etc/passwd", "qa")
        assert "路径越界" in result

    def test_unknown_error_gets_generic_message_in_quick(self):
        result = translate_error("something bizarre happened", "quick")
        assert len(result) > 0

    def test_quick_mode_matches_partial_key(self):
        result = translate_error("命令包含危险操作: rm -rf /", "quick")
        assert "阻止" in result.lower() or "不安全" in result


# ── build_timeline ──


class TestBuildTimeline:
    def test_merges_committed_and_uncommitted(self):
        mock_vcs = MagicMock()
        mock_vcs.log_live_edit_commits.return_value = [
            {"commit_hash": "abc123", "message": "live-edit: fix button", "date": "2026-01-01"},
        ]
        mock_storage = MagicMock()
        mock_storage.get_sessions.return_value = [
            {
                "session_id": "s1",
                "request": "Make it red",
                "committed": 0,
                "commit_hash": "",
                "files": '["a.py"]',
                "created_at": "2026-01-02",
                "mode": "quick",
            },
        ]

        timeline = build_timeline(mock_vcs, mock_storage, limit=30)

        assert len(timeline) >= 1
        committed_hashes = [e["commit_hash"] for e in timeline if e.get("commit_hash")]
        assert "abc123" in committed_hashes

    def test_empty_when_no_data(self):
        mock_vcs = MagicMock()
        mock_vcs.log_live_edit_commits.return_value = []
        mock_storage = MagicMock()
        mock_storage.get_sessions.return_value = []

        timeline = build_timeline(mock_vcs, mock_storage, limit=30)

        assert isinstance(timeline, list)

    def test_uncommitted_have_no_commit_hash(self):
        mock_vcs = MagicMock()
        mock_vcs.log_live_edit_commits.return_value = []
        mock_storage = MagicMock()
        mock_storage.get_sessions.return_value = [
            {
                "session_id": "s1",
                "request": "Test",
                "committed": 0,
                "commit_hash": "",
                "files": '["x.py"]',
                "created_at": "2026-01-01",
                "mode": "quick",
            },
        ]

        timeline = build_timeline(mock_vcs, mock_storage, limit=30)

        uncommitted = [e for e in timeline if not e.get("commit_hash")]
        assert len(uncommitted) == 1
        assert uncommitted[0]["session"]["session_id"] == "s1"


# ── EditSession ──


class TestEditSession:
    def test_init(self):
        session = EditSession("s1", "Fix the header")
        assert session.id == "s1"
        assert session.request == "Fix the header"
        assert session.queue is not None
        assert session._done is False
        assert session._modified_files == []
        assert session.messages == []

    def test_emit_puts_event_on_queue(self):
        session = EditSession("s1", "Fix")
        session.emit("thinking", text="hello")
        event = session.queue.get_nowait()
        assert event["type"] == "thinking"
        assert event["text"] == "hello"

    def test_new_stream_queue_resets_queue(self):
        session = EditSession("s1", "Fix")
        session.queue.put_nowait({"type": "test"})
        session.new_stream_queue()
        assert session.queue.empty()

    def test_cleanup_removes_from_store(self):
        store = SessionStore(max_active=10, ttl_seconds=3600)
        session = EditSession("s1", "Fix")
        store.add(session)
        assert store.get("s1") is session
        session.cleanup(store)
        assert store.get("s1") is None

    async def test_wait_for_approval_approved(self):
        session = EditSession("s1", "Fix")
        session.new_stream_queue()

        async def approve_later():
            await asyncio.sleep(0.05)
            session.approve("t1", True)

        task = asyncio.create_task(approve_later())
        result = await session.wait_for_approval("t1", {"tool": "edit_file"}, timeout=5.0)
        await task

        assert result["approved"] is True

    async def test_wait_for_approval_timeout(self):
        session = EditSession("s1", "Fix")
        session.new_stream_queue()

        result = await session.wait_for_approval("t1", {"tool": "edit_file"}, timeout=0.01)
        assert result["approved"] is False
        assert "超时" in result.get("reason", "")

    def test_auto_approve_off_by_default(self):
        session = EditSession("s1", "Test")
        assert session._auto_approve is False

    async def test_wait_for_approval_auto_approve_returns_immediately(self):
        session = EditSession("s1", "Auto")
        session.set_auto_approve(True)
        result = await session.wait_for_approval("t1", {"tool": "edit_file", "summary": "s"})
        assert result == {"approved": True, "auto": True}
        # The plan event is still emitted so the UI shows what auto-ran.
        event = session.queue.get_nowait()
        assert event["type"] == "tool_plan"
        assert event["id"] == "t1"
        assert event["auto"] is True

    async def test_auto_approve_does_not_emit_approval_wait(self):
        session = EditSession("s1", "Auto")
        session.set_auto_approve(True)
        # Must not block: if it waited, this await would hang the test until timeout.
        result = await session.wait_for_approval("t1", {"tool": "edit_file"}, timeout=0.1)
        assert result["approved"] is True


# ── SessionStore ──


class TestSessionStore:
    def test_add_and_get(self):
        store = SessionStore(max_active=10, ttl_seconds=3600)
        session = EditSession("s1", "Fix")
        store.add(session)
        assert store.get("s1") is session

    def test_get_missing_returns_none(self):
        store = SessionStore(max_active=10, ttl_seconds=3600)
        assert store.get("nonexistent") is None

    def test_remove(self):
        store = SessionStore(max_active=10, ttl_seconds=3600)
        session = EditSession("s1", "Fix")
        store.add(session)
        store.remove("s1")
        assert store.get("s1") is None

    def test_capacity_enforced(self):
        store = SessionStore(max_active=2, ttl_seconds=3600)
        s1 = EditSession("s1", "A")
        s2 = EditSession("s2", "B")
        s3 = EditSession("s3", "C")
        assert store.add(s1) is True
        assert store.add(s2) is True
        assert store.add(s3) is False  # at capacity
        assert store.get("s3") is None

    def test_count(self):
        store = SessionStore(max_active=10, ttl_seconds=3600)
        assert store.count == 0
        store.add(EditSession("s1", "A"))
        assert store.count == 1


# ── run_edit_session (mock provider) ──


class FakeProvider:
    """Provider that returns predetermined content_blocks."""

    def __init__(self, responses: list[list[dict]]):
        self.responses = responses
        self.call_count = 0

    async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
        if self.call_count < len(self.responses):
            result = self.responses[self.call_count]
            self.call_count += 1
            return result
        return [{"type": "text", "text": "Done"}]


class TestRunEditSession:
    @pytest.mark.asyncio
    async def test_text_only_response(self):
        """Session with a provider that returns only text (no tools)."""
        provider = FakeProvider(
            [
                [{"type": "text", "text": "I'll help with that."}],
            ]
        )
        mock_vcs = MagicMock()
        mock_storage = MagicMock()
        mock_storage.save_session = MagicMock()

        config = _make_test_config()

        session = EditSession("s1", "Add a button")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="quick",
            session_store=store,
        )

        events = _drain_queue(session)
        assert any(e["type"] == "done" for e in events)

    @pytest.mark.asyncio
    async def test_tool_execution_read_file(self):
        """Session where the provider calls read_file."""
        import os
        import tempfile

        from live_edit.tool_registry import DefaultToolRegistry

        tmp = tempfile.mkdtemp()
        fpath = os.path.join(tmp, "test.py")
        with open(fpath, "w") as f:
            f.write("print('hello')")

        provider = FakeProvider(
            [
                [
                    {
                        "type": "tool_use",
                        "name": "read_file",
                        "id": "t1",
                        "input": {"path": "test.py"},
                    }
                ],
            ]
        )
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = tmp
        mock_storage = MagicMock()

        registry = DefaultToolRegistry()
        registry.load_builtin_tools()

        config = _make_test_config()
        config.project.root = tmp

        session = EditSession("s1", "Read test.py")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
            tool_registry=registry,
        )

        events = _drain_queue(session)
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0].get("ok") is True

    @pytest.mark.asyncio
    async def test_tool_execution_edit_file(self):
        """Session where the provider edits a file (deep mode, auto-approve)."""
        import os
        import tempfile

        from live_edit.tool_registry import DefaultToolRegistry

        tmp = tempfile.mkdtemp()
        fpath = os.path.join(tmp, "edit_me.py")
        with open(fpath, "w") as f:
            f.write("original content")

        provider = FakeProvider(
            [
                [
                    {
                        "type": "tool_use",
                        "name": "edit_file",
                        "id": "t1",
                        "input": {
                            "path": "edit_me.py",
                            "old_string": "original content",
                            "new_string": "modified content",
                        },
                    }
                ],
            ]
        )
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = tmp
        mock_vcs.commit.return_value = "abc123"
        mock_storage = MagicMock()

        registry = DefaultToolRegistry()
        registry.load_builtin_tools()

        config = _make_test_config()
        config.project.root = tmp

        session = EditSession("s1", "Edit file")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
            tool_registry=registry,
        )

        with open(fpath) as f:
            assert f.read() == "modified content"

    @pytest.mark.asyncio
    async def test_tool_execution_write_file(self):
        """Session where the provider writes a new file."""
        import os
        import tempfile

        from live_edit.tool_registry import DefaultToolRegistry

        tmp = tempfile.mkdtemp()

        provider = FakeProvider(
            [
                [
                    {
                        "type": "tool_use",
                        "name": "write_file",
                        "id": "t1",
                        "input": {"path": "new_file.py", "content": "print('new')"},
                    }
                ],
            ]
        )
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = tmp
        mock_vcs.commit.return_value = "abc123"
        mock_storage = MagicMock()

        registry = DefaultToolRegistry()
        registry.load_builtin_tools()

        config = _make_test_config()
        config.project.root = tmp

        session = EditSession("s1", "Create file")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
            tool_registry=registry,
        )

        assert os.path.exists(os.path.join(tmp, "new_file.py"))

    @pytest.mark.asyncio
    async def test_quick_mode_nudges_on_text_only(self):
        """In quick mode, if no edits made yet, text-only response triggers a nudge."""
        provider = FakeProvider(
            [
                [{"type": "text", "text": "I think you should add a button."}],
                [{"type": "text", "text": "OK, done."}],
            ]
        )
        mock_vcs = MagicMock()
        mock_storage = MagicMock()

        config = _make_test_config()

        session = EditSession("s1", "Add button")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="quick",
            session_store=store,
        )

        assert provider.call_count == 2  # called twice due to nudge

    @pytest.mark.asyncio
    async def test_continue_session(self):
        """continue_edit_session runs the loop on an existing session."""
        provider = FakeProvider(
            [
                [{"type": "text", "text": "Updated."}],
            ]
        )
        mock_vcs = MagicMock()
        mock_storage = MagicMock()

        config = _make_test_config()
        session = EditSession("s1", "Original request")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await continue_edit_session(
            session=session,
            new_request="Change color",
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="quick",
            session_store=store,
        )

        assert session.request == "Change color"

    @pytest.mark.asyncio
    async def test_qa_mode_no_nudge(self):
        """In qa mode, text-only responses should NOT get the code-edit nudge."""
        provider = FakeProvider(
            [
                [{"type": "text", "text": "This project uses FastAPI with SQLite."}],
            ]
        )
        mock_vcs = MagicMock()
        mock_storage = MagicMock()

        config = _make_test_config()

        session = EditSession("s1", "What tech stack?")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="qa",
            session_store=store,
        )

        # In qa mode, should NOT send the "请继续，进行实际的代码修改" nudge
        assert provider.call_count == 1  # Only one call, no nudge loop

    @pytest.mark.asyncio
    async def test_deep_mode_auto_approves_writes(self):
        """In deep mode, write tools are auto-approved (no approval wait)."""
        import os
        import tempfile

        tmp = tempfile.mkdtemp()
        fpath = os.path.join(tmp, "edit_me.py")
        with open(fpath, "w") as f:
            f.write("old")

        provider = FakeProvider(
            [
                [
                    {
                        "type": "tool_use",
                        "name": "edit_file",
                        "id": "t1",
                        "input": {"path": "edit_me.py", "old_string": "old", "new_string": "new"},
                    }
                ],
            ]
        )
        mock_vcs = MagicMock()
        mock_vcs.commit.return_value = "abc"
        mock_storage = MagicMock()

        config = _make_test_config()
        config.project.root = tmp

        session = EditSession("s1", "Edit")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
        )

        events = _drain_queue(session)
        # Should have a tool_plan with auto=True, tool_result, diff, done
        tool_plans = [e for e in events if e["type"] == "tool_plan"]
        assert len(tool_plans) == 1
        assert tool_plans[0].get("auto") is True

    @pytest.mark.asyncio
    async def test_quick_mode_tool_plan_includes_preview_diff(self):
        """quick-mode write approval receives a preview_diff for edit_file."""
        import tempfile
        from types import SimpleNamespace

        edit_root = tempfile.mkdtemp()
        fpath = os.path.join(edit_root, "edit_me.py")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("old\n")

        write_def = SimpleNamespace(is_write=True, require_approval=False)

        class FakeToolRegistry:
            def get_tools(self, mode):
                return ["edit_file"]

            def get_tool(self, name):
                return write_def

            async def execute(self, name, args, root, config):
                return {"ok": True}

        provider = FakeProvider(
            [
                [
                    {
                        "type": "tool_use",
                        "name": "edit_file",
                        "id": "t1",
                        "input": {
                            "path": "edit_me.py",
                            "old_string": "old",
                            "new_string": "new",
                        },
                    }
                ],
                [{"type": "text", "text": "done"}],
            ]
        )

        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = edit_root
        mock_storage = MagicMock()

        config = _make_test_config()
        config.project.root = edit_root

        session = EditSession("s1", "Edit")
        captured = {}

        async def fake_wait(tool_id, tool_data, timeout=300.0):
            captured["data"] = tool_data
            return {"approved": True}

        session.wait_for_approval = fake_wait  # type: ignore[method-assign]

        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="quick",
            session_store=store,
            tool_registry=FakeToolRegistry(),
        )

        assert "preview_diff" in captured["data"]
        assert "-old" in captured["data"]["preview_diff"]
        assert "+new" in captured["data"]["preview_diff"]

    def test_session_store_ttl_expiry(self):
        """Sessions expire after TTL."""
        store = SessionStore(max_active=10, ttl_seconds=0)  # immediate expiry
        session = EditSession("s1", "Test")
        store.add(session)
        # Should be expired immediately
        assert store.get("s1") is None
        assert store.count == 0

    def test_session_store_handles_cleanup_during_add(self):
        """Adding a session triggers stale cleanup."""
        store = SessionStore(max_active=10, ttl_seconds=0)
        s1 = EditSession("s1", "A")
        store._sessions["s1"] = s1  # bypass add to avoid auto-cleanup
        store._sessions["s2"] = EditSession("s2", "B")
        # Now add with ttl=0 should clean up stale
        result = store.add(EditSession("s3", "C"))
        # Stale ones cleaned, new one added
        assert result is True

    def test_translate_error_with_custom_map(self):
        """Custom error map overrides defaults."""
        custom = {"foo error": "bar message"}
        result = translate_error("foo error occurred", "quick", custom_map=custom)
        assert result == "bar message"

    def test_translate_error_no_match_quick(self):
        """In quick mode, unmatched errors get generic wrapper."""
        result = translate_error("untranslatable error XYZ", "quick")
        assert "AI 会自动重试" in result or "untranslatable" in result

    @pytest.mark.asyncio
    async def test_incremental_persist_and_worktree_freshness(self):
        """Mid-run rounds persist the conversation; worktree mtime is refreshed."""
        import tempfile
        import time as _time

        from live_edit.tool_registry import DefaultToolRegistry

        tmp = tempfile.mkdtemp()
        fpath = os.path.join(tmp, "edit_me.py")
        with open(fpath, "w") as f:
            f.write("old")

        provider = FakeProvider(
            [
                [
                    {
                        "type": "tool_use",
                        "name": "edit_file",
                        "id": "t1",
                        "input": {
                            "path": "edit_me.py",
                            "old_string": "old",
                            "new_string": "new",
                        },
                    }
                ],
            ]
        )
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = tmp
        mock_storage = MagicMock()
        registry = DefaultToolRegistry()
        registry.load_builtin_tools()

        config = _make_test_config()
        config.project.root = tmp

        # Backdate the worktree so we can prove the loop refreshed it.
        old = _time.time() - 1000
        os.utime(tmp, (old, old))

        session = EditSession("s1", "edit file")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
            tool_registry=registry,
        )

        calls = mock_storage.save_session.call_args_list
        assert len(calls) >= 2, "expected a mid-loop persist plus the final persist"
        assert "edit_me.py" in calls[-1].kwargs["messages_json"]
        assert os.path.getmtime(tmp) > old


# ── run_edit_session audit + metrics wiring ──


class TestRunEditSessionAuditMetrics:
    @pytest.mark.asyncio
    async def test_tool_execution_records_audit_and_metrics(self, tmp_path):
        """A read_file tool execution records a tool_execution audit + metrics."""
        import os
        import tempfile

        from live_edit.audit import SQLiteAuditLog
        from live_edit.metrics import Metrics
        from live_edit.tool_registry import DefaultToolRegistry

        tmp = tempfile.mkdtemp()
        fpath = os.path.join(tmp, "test.py")
        with open(fpath, "w") as f:
            f.write("print('hello')")

        audit = SQLiteAuditLog(str(tmp_path / "audit.db"))
        metrics = Metrics()

        provider = FakeProvider(
            [
                [
                    {
                        "type": "tool_use",
                        "name": "read_file",
                        "id": "t1",
                        "input": {"path": "test.py"},
                    }
                ],
            ]
        )
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = tmp
        mock_storage = MagicMock()

        registry = DefaultToolRegistry()
        registry.load_builtin_tools()

        config = _make_test_config()
        config.project.root = tmp

        session = EditSession("s1", "Read test.py")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
            tool_registry=registry,
            audit_log=audit,
            metrics=metrics,
        )

        events = audit.query(action="tool_execution")
        assert len(events) == 1
        assert events[0].detail.get("tool") == "read_file"
        assert events[0].result == "ok"

        rendered = metrics.render()
        assert 'live_edit_tool_executions_total{status="ok",tool="read_file"} 1' in rendered
        assert 'live_edit_llm_calls_total{status="ok"}' in rendered
        assert session._outcome == "completed"

    @pytest.mark.asyncio
    async def test_session_completed_records_audit_and_metrics(self, tmp_path):
        """A session that finishes without edits records session_completed."""
        from live_edit.audit import SQLiteAuditLog
        from live_edit.metrics import Metrics

        audit = SQLiteAuditLog(str(tmp_path / "audit.db"))
        metrics = Metrics()

        provider = FakeProvider(
            [
                [{"type": "text", "text": "I'll help with that."}],
            ]
        )
        mock_vcs = MagicMock()
        mock_storage = MagicMock()
        mock_storage.save_session = MagicMock()

        config = _make_test_config()

        session = EditSession("s1", "Add a button")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="quick",
            session_store=store,
            audit_log=audit,
            metrics=metrics,
        )

        events = audit.query(action="session_completed")
        assert len(events) == 1
        assert events[0].session_id == "s1"
        assert session._outcome == "completed"

        rendered = metrics.render()
        assert 'live_edit_sessions_total{outcome="completed"} 1' in rendered
        assert 'live_edit_llm_calls_total{status="ok"}' in rendered

    @pytest.mark.asyncio
    async def test_session_error_records_failed_outcome(self, tmp_path):
        """A provider exception records session_failed + error LLM metrics."""
        from live_edit.audit import SQLiteAuditLog
        from live_edit.metrics import Metrics

        audit = SQLiteAuditLog(str(tmp_path / "audit.db"))
        metrics = Metrics()

        class BoomProvider:
            async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
                raise RuntimeError("provider exploded")

        mock_vcs = MagicMock()
        mock_storage = MagicMock()

        config = _make_test_config()

        session = EditSession("s1", "boom")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=BoomProvider(),
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="quick",
            session_store=store,
            audit_log=audit,
            metrics=metrics,
        )

        events = audit.query(action="session_failed")
        assert len(events) == 1
        assert events[0].session_id == "s1"
        assert session._outcome == "failed"

        rendered = metrics.render()
        assert 'live_edit_sessions_total{outcome="failed"} 1' in rendered
        assert 'live_edit_llm_calls_total{status="error"} 1' in rendered
        assert 'live_edit_errors_total{error_type="RuntimeError"} 1' in rendered

    @pytest.mark.asyncio
    async def test_approval_timeout_records_audit_and_metric(self, tmp_path):
        """A per-tool approval timeout records approve/timeout audit + metric."""
        import tempfile
        from types import SimpleNamespace

        from live_edit.audit import SQLiteAuditLog
        from live_edit.metrics import Metrics

        audit = SQLiteAuditLog(str(tmp_path / "audit.db"))
        metrics = Metrics()

        write_def = SimpleNamespace(is_write=True, require_approval=False)

        class FakeToolRegistry:
            def get_tools(self, mode):
                return ["write_file"]

            def get_tool(self, name):
                return write_def

            async def execute(self, name, args, root, config):
                return {"ok": True}

        provider = FakeProvider(
            [
                [
                    {
                        "type": "tool_use",
                        "name": "write_file",
                        "id": "t1",
                        "input": {"path": "a.py", "content": "x"},
                    }
                ],
                [{"type": "text", "text": "done"}],
            ]
        )

        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = tempfile.mkdtemp()
        mock_storage = MagicMock()

        config = _make_test_config()

        session = EditSession("s1", "Add a file")
        session.wait_for_approval = AsyncMock(
            return_value={"approved": False, "reason": "用户超时未响应"}
        )
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="quick",
            session_store=store,
            tool_registry=FakeToolRegistry(),
            audit_log=audit,
            metrics=metrics,
        )

        events = audit.query(action="approve")
        assert len(events) == 1
        assert events[0].result == "timeout"
        assert events[0].target == "t1"
        assert events[0].session_id == "s1"

        rendered = metrics.render()
        assert 'live_edit_approvals_total{decision="timeout"} 1' in rendered

    @pytest.mark.asyncio
    async def test_fix_loop_records_tool_audit_and_metrics(self, tmp_path):
        """The evaluation fix loop wraps tool execution + LLM calls with audit/metrics."""
        from live_edit.audit import SQLiteAuditLog
        from live_edit.engine import _run_agent_loop_fix
        from live_edit.metrics import Metrics

        audit = SQLiteAuditLog(str(tmp_path / "audit.db"))
        metrics = Metrics()

        class FakeToolRegistry:
            def get_tools(self, mode):
                return ["edit_file"]

            async def execute(self, name, args, root, config):
                return {"ok": True}

        session = EditSession("s1", "fix")
        session._mode = "deep"
        session._worktree_path = "/tmp/nowhere"

        provider = FakeProvider(
            [
                [
                    {
                        "type": "tool_use",
                        "name": "edit_file",
                        "id": "t1",
                        "input": {"path": "x.py"},
                    }
                ],
            ]
        )

        await _run_agent_loop_fix(
            session=session,
            provider=provider,
            config=_make_test_config(),
            tool_registry=FakeToolRegistry(),
            max_rounds=2,
            audit_log=audit,
            metrics=metrics,
        )

        events = audit.query(action="tool_execution")
        assert len(events) == 1
        assert events[0].detail.get("tool") == "edit_file"
        assert events[0].result == "ok"

        rendered = metrics.render()
        assert 'live_edit_tool_executions_total{status="ok",tool="edit_file"} 1' in rendered
        assert 'live_edit_llm_calls_total{status="ok"} 2' in rendered


# ── helpers ──


def _make_test_config():
    """Build a minimal Config for testing."""
    return Config(
        project=ProjectConfig(name="test", language="python", root="."),
        llm=LLMConfig(api_url="https://example.com", api_key_env="KEY", model="test"),
        safety=SafetyConfig(),
        timeouts=TimeoutsConfig(),
        sessions=SessionsConfig(),
        hooks=HooksConfig(),
        ui=UIConfig(),
        modes={
            "quick": ModeConfig(
                label="快速修改",
                approval="per_tool",
                tools="write",
                approve_for=["edit_file", "write_file"],
                prompt=ModePromptConfig(
                    base="You are a helpful dev.",
                    user_persona="Non-technical user.",
                    communication_rules="Use Chinese.",
                ),
            ),
            "deep": ModeConfig(
                label="深度开发",
                approval="final",
                tools="all",
                approve_for=[],
                prompt=ModePromptConfig(
                    base="You are a dev assistant.",
                    user_persona="Developer.",
                    communication_rules="Use technical terms.",
                ),
            ),
        },
        errors=ErrorTranslations(quick={}, deep={}),
        evaluation=EvaluationConfig(enabled=False),
    )


def _drain_queue(session: EditSession) -> list[dict]:
    events = []
    while not session.queue.empty():
        try:
            event = session.queue.get_nowait()
            if event is not None:
                events.append(event)
        except asyncio.QueueEmpty:
            break
    return events


class _FakeSession:
    """Minimal session-like object for _do_commit testing."""

    def __init__(self, sid, wt_path, files):
        self.id = sid
        self.request = "test request"
        self._worktree_path = wt_path
        self._modified_files = files
        self._merged = False
        self._committed = False
        self._commit_hash = ""
        self._done = False
        self._mode = "quick"
        self._created_at = 0
        self._preview_url = ""
        self.emitted = []
        self.messages = []

    def emit(self, event, **kw):
        self.emitted.append((event, kw))


class TestDoCommitBranchOnly:
    @staticmethod
    def _make_repo(tmp_path):
        """Init a real git repo and create a worktree; returns (repo, vcs, wt_path)."""
        from live_edit.vcs import GitVCS

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), capture_output=True)
        subprocess.run(  # noqa: E501
            ["git", "config", "user.email", "t@t.com"],
            cwd=str(repo),
            capture_output=True,
        )
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
        (repo / "init.txt").write_text("init")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo), capture_output=True)
        vcs = GitVCS(repo)
        wt_path = vcs.create_worktree("sess-bonly")
        return repo, vcs, wt_path

    def test_commit_keeps_branch_does_not_merge(self, tmp_path):
        from unittest.mock import MagicMock

        from live_edit.engine import _do_commit

        repo, vcs, wt_path = self._make_repo(tmp_path)
        main_before = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        ).stdout.strip()

        (Path(wt_path) / "new.py").write_text("print(1)")
        session = _FakeSession("sess-bonly", wt_path, ["new.py"])

        storage = MagicMock()
        config = MagicMock()
        config.hooks = None

        import asyncio

        asyncio.run(_do_commit(session, vcs, storage, config))

        # main 未移动
        main_after = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert main_before == main_after

        # 分支存在
        branches = subprocess.run(
            ["git", "branch", "--list", "live-edit/sess-bonly"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        ).stdout
        assert "live-edit/sess-bonly" in branches

        # worktree 目录已删
        assert not os.path.isdir(wt_path)

        # done 事件 message 含分支名
        done_events = [kw for ev, kw in session.emitted if ev == "done"]
        assert done_events, "expected a done event"
        assert any("live-edit/sess-bonly" in kw.get("message", "") for kw in done_events)
        assert session._committed is True

    def test_commit_records_audit_action(self, tmp_path):
        """A successful _do_commit records a single commit audit event."""
        from unittest.mock import MagicMock

        from live_edit.audit import SQLiteAuditLog
        from live_edit.engine import _do_commit

        repo, vcs, wt_path = self._make_repo(tmp_path)
        (Path(wt_path) / "new.py").write_text("print(1)")
        session = _FakeSession("sess-bonly", wt_path, ["new.py"])

        storage = MagicMock()
        config = MagicMock()
        config.hooks = None

        audit = SQLiteAuditLog(str(tmp_path / "audit.db"))

        import asyncio

        asyncio.run(_do_commit(session, vcs, storage, config, audit_log=audit))

        assert session._commit_hash, "expected a commit hash on success"
        events = audit.query(action="commit")
        assert len(events) == 1
        assert events[0].result == "ok"
        assert events[0].target == session._commit_hash


class TestFormatMemoryContext:
    def test_default_template(self):
        from live_edit.memory import MemoryEntry, _format_memory_context

        memories = [
            MemoryEntry(
                session_id="s1",
                request="Fix auth",
                file_path="auth.py",
                diff_summary="+import jwt\n+def login():",
                stat="+3/-1",
                commit_hash="abc",
                score=0.95,
            ),
        ]
        result = _format_memory_context(memories)
        assert "Relevant Past Changes" in result
        assert "Fix auth" in result
        assert "auth.py" in result
        assert "95%" in result

    def test_custom_template(self):
        from live_edit.memory import MemoryEntry, _format_memory_context

        memories = [
            MemoryEntry(
                session_id="s1",
                request="Fix auth",
                file_path="auth.py",
                diff_summary="+import jwt",
                stat="+3/-1",
                commit_hash="abc",
                score=0.95,
            ),
        ]
        template = "[{index}] {request} {file} {stat} {score}"
        result = _format_memory_context(memories, template)
        assert "[1] Fix auth auth.py +3/-1 95%" in result

    def test_empty_file_path_shows_request_only(self):
        from live_edit.memory import MemoryEntry, _format_memory_context

        memories = [
            MemoryEntry(
                session_id="s1",
                request="Some query",
                file_path="",
                diff_summary="",
                stat="",
                commit_hash="",
                score=0.80,
            ),
        ]
        result = _format_memory_context(memories)
        assert "Some query" in result

    def test_empty_memories(self):
        from live_edit.memory import _format_memory_context

        result = _format_memory_context([])
        assert "Relevant" in result
        assert "Use the above" in result


class TestSessionMemoryEngineIntegration:
    """Integration tests for session memory in the engine."""

    @pytest.mark.asyncio
    async def test_session_memory_disabled_by_default(self):
        """When session_memory is disabled, no memory injection or errors."""
        from unittest.mock import MagicMock

        from live_edit.config import Config
        from live_edit.engine import EditSession, run_edit_session

        config = Config()
        session = EditSession("test-s1", "Make it red")
        mock_provider = AsyncMock()
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = "/tmp/test-s1"
        mock_vcs.commit_in_worktree.return_value = "fakehash"
        mock_storage = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_tools.return_value = []

        mock_provider.call_with_tools.return_value = [
            {"type": "text", "text": "I'll make it red."},
        ]

        await run_edit_session(
            session=session,
            provider=mock_provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            tool_registry=mock_registry,
        )
        # Should complete without errors
        assert True

    @pytest.mark.asyncio
    async def test_missing_rag_dependency_logs_warning(self):
        """When rag dep is missing, engine should warn but not crash."""
        from unittest.mock import MagicMock

        from live_edit.config import Config
        from live_edit.engine import EditSession, run_edit_session

        config = Config()
        config.session_memory.enabled = True
        config.memory.enabled = True

        session = EditSession("test-s2", "Make it red")
        mock_provider = AsyncMock()
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = "/tmp/test-s2"
        mock_vcs.commit_in_worktree.return_value = "fakehash"
        mock_storage = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_tools.return_value = []

        mock_provider.call_with_tools.return_value = [
            {"type": "text", "text": "Done."},
        ]

        with patch("live_edit.embedder.LocalEmbedder", side_effect=ImportError("No module")):
            await run_edit_session(
                session=session,
                provider=mock_provider,
                vcs=mock_vcs,
                storage=mock_storage,
                config=config,
                mode="deep",
                tool_registry=mock_registry,
            )
        # Should complete without raising


@pytest.mark.asyncio
async def test_memory_manager_integration():
    """Engine constructs MemoryManager when memory.enabled; leaves None when disabled."""
    from live_edit.config import Config, LongTermConfig, MemoryConfig
    from live_edit.engine import EditSession, run_edit_session
    from live_edit.memory import MemoryManager

    class FakeEmbedder:
        def embed(self, text):
            return [0.5] * 384

        def embed_batch(self, texts):
            return [[0.5] * 384 for _ in texts]

        @property
        def dimension(self):
            return 384

    async def run_session(memory_enabled):
        config = Config(
            memory=MemoryConfig(
                enabled=memory_enabled,
                long_term=LongTermConfig(enabled=False),
            ),
        )
        session = EditSession(f"test-s3-{memory_enabled}", "Make it red")
        mock_provider = AsyncMock()
        mock_provider.call_with_tools.return_value = [{"type": "text", "text": "Done."}]
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = "/tmp/test-s3"
        mock_vcs.commit_in_worktree.return_value = "fakehash"
        mock_storage = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_tools.return_value = []

        with patch("live_edit.embedder.LocalEmbedder", return_value=FakeEmbedder()):
            await run_edit_session(
                session=session,
                provider=mock_provider,
                vcs=mock_vcs,
                storage=mock_storage,
                config=config,
                mode="deep",
                tool_registry=mock_registry,
            )
        return session

    enabled_session = await run_session(True)
    assert isinstance(enabled_session._session_memory, MemoryManager)

    disabled_session = await run_session(False)
    assert disabled_session._session_memory is None


class TestRehydrateSession:
    def test_restores_fields_and_strips_orphan_tool_use(self):
        from live_edit.engine import rehydrate_session

        detail = {
            "request": "polish doc",
            "mode": "deep",
            "committed": 0,
            "commit_hash": "",
            "files": ["doc.md"],
            "messages": [
                {"role": "user", "content": "polish"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "orphan",
                            "name": "edit_file",
                            "input": {"path": "doc.md"},
                        }
                    ],
                },
            ],
        }
        session = rehydrate_session("s-rehydrate", detail)
        assert session is not None
        assert session.id == "s-rehydrate"
        assert session.request == "polish doc"
        assert session._mode == "deep"
        assert session._modified_files == ["doc.md"]
        # The unpaired tool_use from a crash window must be stripped.
        leftovers = [
            b
            for m in session.messages
            if isinstance(m.get("content"), list)
            for b in m["content"]
            if b.get("type") == "tool_use"
        ]
        assert leftovers == []
        assert session._committed is False
        assert session._commit_hash == ""

    def test_reuses_surviving_worktree(self, tmp_path, monkeypatch):
        import live_edit.vcs as vcs_mod
        from live_edit.engine import rehydrate_session

        sid = "s-wt"
        wt = tmp_path / sid
        wt.mkdir()
        monkeypatch.setattr(vcs_mod, "_WORKTREE_ROOT", str(tmp_path))
        detail = {
            "request": "r",
            "mode": "quick",
            "committed": 0,
            "commit_hash": "",
            "files": [],
            "messages": [{"role": "user", "content": "hi"}],
        }
        session = rehydrate_session(sid, detail)
        assert session._worktree_path == str(wt)
        assert session._merged is False

    def test_returns_none_without_messages(self):
        from live_edit.engine import rehydrate_session

        assert rehydrate_session("s-empty", {"request": "x", "messages": []}) is None

    @pytest.mark.asyncio
    async def test_continue_recovered_committed_session_keeps_branch(self, tmp_path, monkeypatch):
        """Regression: a committed session recovered via /continue that ends
        without a new commit must keep its branch. Previously the finally block
        discarded it because rehydrate resets _merged, losing the committed work."""
        import live_edit.vcs as vcs_mod
        from live_edit.engine import continue_edit_session, rehydrate_session
        from live_edit.tool_registry import DefaultToolRegistry

        repo = str(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
        (tmp_path / "initial.txt").write_text("initial")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, check=True
        )

        monkeypatch.setattr(vcs_mod, "_WORKTREE_ROOT", str(tmp_path / "wt-root"))
        vcs = vcs_mod.GitVCS(repo)
        sid = "s-committed-recover"
        wt = vcs.create_worktree(sid)
        # Commit real work to the session branch (mirrors a completed _do_commit).
        (Path(wt) / "work.txt").write_text("done")
        subprocess.run(
            ["git", "-C", wt, "add", "work.txt"], capture_output=True, text=True, check=True
        )
        subprocess.run(
            ["git", "-C", wt, "commit", "-m", "live-edit: session work"],
            capture_output=True,
            text=True,
            check=True,
        )
        commit_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True, check=True
        ).stdout.strip()

        detail = {
            "request": "original",
            "mode": "quick",
            "committed": 1,
            "commit_hash": commit_hash,
            "files": ["work.txt"],
            "messages": [{"role": "user", "content": "do the work"}],
        }
        session = rehydrate_session(sid, detail)
        assert session is not None
        assert session._committed is True

        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)
        config = _make_test_config()
        registry = DefaultToolRegistry()
        registry.load_builtin_tools()
        provider = FakeProvider([[{"type": "text", "text": "x" * 300}]])
        mock_storage = MagicMock()

        await continue_edit_session(
            session=session,
            new_request="actually nothing",
            provider=provider,
            vcs=vcs,
            storage=mock_storage,
            config=config,
            mode="quick",
            session_store=store,
            tool_registry=registry,
        )

        branches = subprocess.run(
            ["git", "-C", repo, "branch", "--list", f"live-edit/{sid}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert branches != ""
        # Worktree dir should be reclaimed while the branch is kept.
        assert not os.path.isdir(wt)

        # Cleanup the branch the test created.
        vcs.discard_session_branch(sid)


class TestRollbackAudit:
    @pytest.mark.asyncio
    async def test_terminal_reject_records_rollback_audit(self, tmp_path):
        """Rejecting the final approval records a rollback audit event."""
        from unittest.mock import MagicMock

        from live_edit.audit import SQLiteAuditLog
        from live_edit.config import ModeConfig, ModePromptConfig
        from live_edit.engine import EditSession, SessionStore, run_edit_session
        from live_edit.vcs import GitVCS

        # Real git repo so worktree + diff generation work.
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), capture_output=True)
        subprocess.run(  # noqa: E501
            ["git", "config", "user.email", "t@t.com"],
            cwd=str(repo),
            capture_output=True,
        )
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
        (repo / "init.txt").write_text("init")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo), capture_output=True)
        vcs = GitVCS(repo)

        audit = SQLiteAuditLog(str(tmp_path / "audit.db"))

        config = _make_test_config()
        config.modes["final_approve"] = ModeConfig(
            label="Final",
            approval="final",
            tools="write",
            approve_for=[],
            prompt=ModePromptConfig(
                base="You are a dev.",
                user_persona="Developer.",
                communication_rules="Use Chinese.",
            ),
        )

        class FakeToolRegistry:
            def get_tools(self, mode):
                return ["write_file"]

            def get_tool(self, name):
                return None

            async def execute(self, name, args, root, config):
                (Path(root) / args["path"]).write_text("content")
                return {"ok": True}

        provider = FakeProvider(
            [
                [
                    {
                        "type": "tool_use",
                        "name": "write_file",
                        "id": "t1",
                        "input": {"path": "a.py", "content": "x"},
                    }
                ],
                [{"type": "text", "text": "done"}],
            ]
        )

        mock_storage = MagicMock()
        mock_storage.save_session = MagicMock()

        session = EditSession("s-rollback", "Add a file")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        task = asyncio.create_task(
            run_edit_session(
                session=session,
                provider=provider,
                vcs=vcs,
                storage=mock_storage,
                config=config,
                mode="final_approve",
                session_store=store,
                tool_registry=FakeToolRegistry(),
                audit_log=audit,
            )
        )

        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10
            approved = False
            while loop.time() < deadline:
                try:
                    event = session.queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.05)
                    continue
                if (
                    event is not None
                    and event.get("type") == "tool_plan"
                    and event.get("id") == "__final__"
                ):
                    session.approve("__final__", False)
                    approved = True
                    break
            if not approved:
                pytest.fail("timed out waiting for __final__ tool_plan")
            await task
        finally:
            if not task.done():
                task.cancel()

        events = audit.query(action="rollback")
        assert len(events) == 1
        assert events[0].result == "ok"
        assert events[0].target == session.id
        assert session._outcome == "cancelled"


# ── Evaluation diff population ──


@pytest.mark.asyncio
class TestEvalDiffPopulation:
    async def test_eval_populates_cached_diff_before_first_run(self, tmp_path, monkeypatch):
        import subprocess as sp

        import live_edit.engine as eng
        from live_edit.config import EvaluationConfig
        from live_edit.evaluation import EvalResult

        sp.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        p = tmp_path / "a.py"
        p.write_text("x = 1\n")
        sp.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
        sp.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
            cwd=str(tmp_path),
            check=True,
        )
        p.write_text("x = 2\n")  # uncommitted change

        captured = {}

        async def _fake_pipeline(session, provider, config, preview_manager=None):
            captured["diff"] = session._cached_diff
            return EvalResult(passed=True)

        monkeypatch.setattr(eng, "run_evaluation_pipeline", _fake_pipeline)

        config = _make_test_config()
        config.evaluation = EvaluationConfig(enabled=True, max_retries=0, stages=["introspect"])
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = str(tmp_path)
        mock_storage = MagicMock()
        mock_storage.save_session = MagicMock()

        session = EditSession("s1", "change x")
        session._modified_files = ["a.py"]
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=FakeProvider([[{"type": "text", "text": "Done"}]]),
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
        )
        assert "x = 2" in captured["diff"]


# ── eval failure note ──


class TestEvalFailureNote:
    @pytest.mark.asyncio
    async def test_eval_failure_appends_conversation_note(self, monkeypatch):
        import live_edit.engine as eng
        from live_edit.config import EvaluationConfig
        from live_edit.evaluation import EvalResult

        config = _make_test_config()
        config.evaluation = EvaluationConfig(enabled=True, max_retries=0, stages=["lint"])

        async def _fake_pipeline(session, provider, config, preview_manager=None):
            return EvalResult(passed=False, failed_stage="test", failed_output="boom")

        monkeypatch.setattr(eng, "run_evaluation_pipeline", _fake_pipeline)

        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = "/tmp/evalnote"
        mock_storage = MagicMock()
        mock_storage.save_session = MagicMock()

        session = EditSession("s1", "fix it")
        session._modified_files = ["x.py"]
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=FakeProvider([[{"type": "text", "text": "Done"}]]),
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
        )

        events = _drain_queue(session)
        assert any(e["type"] == "text" and "自动检查没通过" in e.get("text", "") for e in events)
        assert any(e["type"] == "text" and "测试" in e.get("text", "") for e in events)
        last = session.messages[-1]
        assert last["role"] == "assistant"
        assert "自动检查没通过" in last["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_eval_passed_no_note(self, monkeypatch):
        import live_edit.engine as eng
        from live_edit.config import EvaluationConfig
        from live_edit.evaluation import EvalResult

        config = _make_test_config()
        config.evaluation = EvaluationConfig(enabled=True, max_retries=0, stages=["lint"])

        async def _fake_pipeline(session, provider, config, preview_manager=None):
            return EvalResult(passed=True)

        monkeypatch.setattr(eng, "run_evaluation_pipeline", _fake_pipeline)

        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = "/tmp/evalok"
        mock_storage = MagicMock()
        mock_storage.save_session = MagicMock()

        session = EditSession("s1", "fix it")
        session._modified_files = ["x.py"]
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=FakeProvider([[{"type": "text", "text": "Done"}]]),
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
        )

        events = _drain_queue(session)
        assert not any(
            e["type"] == "text" and "自动检查没通过" in e.get("text", "") for e in events
        )

    def test_eval_failure_note_plain(self):
        from live_edit.engine import _eval_failure_note

        note = _eval_failure_note("test")
        assert "测试" in note
        assert "自动检查没通过" in note
        assert _eval_failure_note("unknown_stage").startswith("不过")
