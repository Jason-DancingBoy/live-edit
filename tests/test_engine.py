"""Tests for live_edit.engine — EditSession, agent loop, timeline, error translation."""

import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from live_edit.engine import (
    EditSession,
    SessionStore,
    translate_error,
    build_timeline,
    run_edit_session,
    continue_edit_session,
)
from live_edit.config import (
    Config, ModeConfig, ModePromptConfig, ProjectConfig, LLMConfig,
    SafetyConfig, TimeoutsConfig, SessionsConfig, HooksConfig, UIConfig,
    ErrorTranslations,
)
from live_edit.vcs import RevertPreview, RevertResult


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
            {"session_id": "s1", "request": "Make it red", "committed": 0,
             "commit_hash": "", "files": '["a.py"]', "created_at": "2026-01-02",
             "mode": "quick"},
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
            {"session_id": "s1", "request": "Test", "committed": 0,
             "commit_hash": "", "files": '["x.py"]', "created_at": "2026-01-01",
             "mode": "quick"},
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
        provider = FakeProvider([
            [{"type": "text", "text": "I'll help with that."}],
        ])
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
        import tempfile, os
        tmp = tempfile.mkdtemp()
        fpath = os.path.join(tmp, "test.py")
        with open(fpath, "w") as f:
            f.write("print('hello')")

        provider = FakeProvider([
            [{"type": "tool_use", "name": "read_file", "id": "t1",
              "input": {"path": "test.py"}}],
        ])
        mock_vcs = MagicMock()
        mock_storage = MagicMock()

        config = _make_test_config()
        config.project.root = tmp

        session = EditSession("s1", "Read test.py")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session, provider=provider, vcs=mock_vcs,
            storage=mock_storage, config=config, mode="deep",
            session_store=store,
        )

        events = _drain_queue(session)
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0].get("ok") is True

    @pytest.mark.asyncio
    async def test_tool_execution_edit_file(self):
        """Session where the provider edits a file (deep mode, auto-approve)."""
        import tempfile, os
        tmp = tempfile.mkdtemp()
        fpath = os.path.join(tmp, "edit_me.py")
        with open(fpath, "w") as f:
            f.write("original content")

        provider = FakeProvider([
            [{"type": "tool_use", "name": "edit_file", "id": "t1",
              "input": {"path": "edit_me.py", "old_string": "original content",
                        "new_string": "modified content"}}],
        ])
        mock_vcs = MagicMock()
        mock_vcs.commit.return_value = "abc123"
        mock_storage = MagicMock()

        config = _make_test_config()
        config.project.root = tmp

        session = EditSession("s1", "Edit file")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session, provider=provider, vcs=mock_vcs,
            storage=mock_storage, config=config, mode="deep",
            session_store=store,
        )

        with open(fpath) as f:
            assert f.read() == "modified content"

    @pytest.mark.asyncio
    async def test_tool_execution_write_file(self):
        """Session where the provider writes a new file."""
        import tempfile, os
        tmp = tempfile.mkdtemp()

        provider = FakeProvider([
            [{"type": "tool_use", "name": "write_file", "id": "t1",
              "input": {"path": "new_file.py", "content": "print('new')"}}],
        ])
        mock_vcs = MagicMock()
        mock_vcs.commit.return_value = "abc123"
        mock_storage = MagicMock()

        config = _make_test_config()
        config.project.root = tmp

        session = EditSession("s1", "Create file")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session, provider=provider, vcs=mock_vcs,
            storage=mock_storage, config=config, mode="deep",
            session_store=store,
        )

        assert os.path.exists(os.path.join(tmp, "new_file.py"))

    @pytest.mark.asyncio
    async def test_quick_mode_nudges_on_text_only(self):
        """In quick mode, if no edits made yet, text-only response triggers a nudge."""
        provider = FakeProvider([
            [{"type": "text", "text": "I think you should add a button."}],
            [{"type": "text", "text": "OK, done."}],
        ])
        mock_vcs = MagicMock()
        mock_storage = MagicMock()

        config = _make_test_config()

        session = EditSession("s1", "Add button")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session, provider=provider, vcs=mock_vcs,
            storage=mock_storage, config=config, mode="quick",
            session_store=store,
        )

        assert provider.call_count == 2  # called twice due to nudge

    @pytest.mark.asyncio
    async def test_continue_session(self):
        """continue_edit_session runs the loop on an existing session."""
        provider = FakeProvider([
            [{"type": "text", "text": "Updated."}],
        ])
        mock_vcs = MagicMock()
        mock_storage = MagicMock()

        config = _make_test_config()
        session = EditSession("s1", "Original request")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await continue_edit_session(
            session=session, new_request="Change color",
            provider=provider, vcs=mock_vcs,
            storage=mock_storage, config=config, mode="quick",
            session_store=store,
        )

        assert session.request == "Change color"

    @pytest.mark.asyncio
    async def test_qa_mode_no_nudge(self):
        """In qa mode, text-only responses should NOT get the code-edit nudge."""
        provider = FakeProvider([
            [{"type": "text", "text": "This project uses FastAPI with SQLite."}],
        ])
        mock_vcs = MagicMock()
        mock_storage = MagicMock()

        config = _make_test_config()

        session = EditSession("s1", "What tech stack?")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session, provider=provider, vcs=mock_vcs,
            storage=mock_storage, config=config, mode="qa",
            session_store=store,
        )

        # In qa mode, should NOT send the "请继续，进行实际的代码修改" nudge
        assert provider.call_count == 1  # Only one call, no nudge loop

    @pytest.mark.asyncio
    async def test_deep_mode_auto_approves_writes(self):
        """In deep mode, write tools are auto-approved (no approval wait)."""
        import tempfile, os
        tmp = tempfile.mkdtemp()
        fpath = os.path.join(tmp, "edit_me.py")
        with open(fpath, "w") as f:
            f.write("old")

        provider = FakeProvider([
            [{"type": "tool_use", "name": "edit_file", "id": "t1",
              "input": {"path": "edit_me.py", "old_string": "old", "new_string": "new"}}],
        ])
        mock_vcs = MagicMock()
        mock_vcs.commit.return_value = "abc"
        mock_storage = MagicMock()

        config = _make_test_config()
        config.project.root = tmp

        session = EditSession("s1", "Edit")
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session, provider=provider, vcs=mock_vcs,
            storage=mock_storage, config=config, mode="deep",
            session_store=store,
        )

        events = _drain_queue(session)
        # Should have a tool_plan with auto=True, tool_result, diff, done
        tool_plans = [e for e in events if e["type"] == "tool_plan"]
        assert len(tool_plans) == 1
        assert tool_plans[0].get("auto") is True

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
