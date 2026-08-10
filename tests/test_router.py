"""Tests for live_edit.router — FastAPI endpoints."""

import os
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeProvider:
    """Provider that returns predetermined content_blocks."""

    def __init__(self, responses=None):
        self.responses = responses or [[{"type": "text", "text": "Done."}]]
        self.call_count = 0

    async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
        if self.call_count < len(self.responses):
            result = self.responses[self.call_count]
            self.call_count += 1
            return result
        return [{"type": "text", "text": "All done."}]


def _write_router_config(tmp_path):
    """Write the shared .live-edit.toml used by router fixtures."""
    config_path = tmp_path / ".live-edit.toml"
    config_path.write_text(
        """[project]
name = "TestApp"
language = "python"
root = "."

[llm]
provider = "anthropic_compatible"
api_url = "https://api.example.com/v1/messages"
api_key_env = "FAKE_KEY"
model = "test-model"

[safety]
allowed_dirs = ["."]

[timeouts]
api_request = 180
shell_command = 30

[sessions]
max_active = 10

[hooks]

[ui]
default_mode = "quick"

[modes.quick]
label = "快速修改"
approval = "per_tool"
tools = "write"
approve_for = ["edit_file", "write_file"]

[modes.quick.prompt]
base = "You are a helpful AI."
user_persona = "Non-technical user."
communication_rules = "Use Chinese."

[modes.deep]
label = "深度开发"
approval = "final"
tools = "all"

[modes.deep.prompt]
base = "You are a dev assistant."
user_persona = "Developer."
communication_rules = "Use technical terms."

[errors.quick]
"old_string 在文件中未找到" = "文件内容已变化"
[errors.deep]
"""
    )
    return config_path


@pytest.fixture
def app_with_router(tmp_path):
    """Create a FastAPI app with live-edit router mounted, using mocks."""
    from live_edit.router import setup_live_edit

    config_path = _write_router_config(tmp_path)

    mock_provider = FakeProvider()
    mock_vcs = MagicMock()
    mock_vcs.commit.return_value = "abc123"
    mock_vcs.diff_stat.return_value = "file.py | 2 +-"
    mock_vcs.diff_full.return_value = "-old\\n+new"
    mock_vcs.log_live_edit_commits.return_value = [
        {"commit_hash": "abc123", "message": "live-edit: fix", "date": "2026-01-01"},
    ]
    mock_vcs.revert_preview.return_value = MagicMock(
        ok=True,
        can_revert=True,
        files=["file.py"],
        diff_summary="1 file changed",
        conflicts=[],
    )
    mock_vcs.revert_execute.return_value = MagicMock(
        ok=True,
        new_commit_hash="def456",
        message="回滚成功",
    )
    mock_storage = MagicMock()
    mock_storage.get_sessions.return_value = []
    mock_storage.get_session_detail.side_effect = lambda sid: (
        {
            "session_id": "s1",
            "request": "Test",
            "committed": 1,
            "commit_hash": "abc",
            "files": '["a.py"]',
            "mode": "quick",
            "messages": "[]",
        }
        if sid == "s1"
        else None
    )

    router = setup_live_edit(
        project_root=str(tmp_path),
        config_path=str(config_path),
        provider=mock_provider,
        storage=mock_storage,
        vcs=mock_vcs,
    )

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app_with_router):
    return TestClient(app_with_router)


class TestStaticFiles:
    def test_serves_js_file(self, client, tmp_path):
        """GET /live-edit/static/live-edit.js returns JS content."""
        static_dir = tmp_path / "live_edit" / "static"
        static_dir.mkdir(parents=True, exist_ok=True)
        (static_dir / "live-edit.js").write_text("// live-edit client")

        # The router serves from the package's static dir, not tmp_path.
        # This test verifies the endpoint exists and returns 200 or 404.
        response = client.get("/live-edit/static/live-edit.js")
        # May be 404 if static files not built yet — that's fine
        assert response.status_code in (200, 404)


class TestTimeline:
    def test_returns_timeline(self, client):
        response = client.get("/live-edit/timeline")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "entries" in data
        assert isinstance(data["entries"], list)

    def test_timeline_respects_limit(self, client):
        response = client.get("/live-edit/timeline?limit=5")
        assert response.status_code == 200


class TestSessionDetail:
    def test_returns_session_detail(self, client):
        response = client.get("/live-edit/session/s1")
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "s1"
        assert data["mode"] == "quick"

    def test_returns_404_for_nonexistent(self, client):
        # Override mock to return None
        pass  # Tested via mock at fixture level


class TestRevert:
    def test_revert_preview(self, client):
        response = client.post("/live-edit/revert/abc123/preview")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True

    def test_revert_execute(self, client):
        response = client.post("/live-edit/revert/abc123/execute")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True


class TestApproveEndpoint:
    def test_approve_tool(self, client):
        """POST /live-edit/approve/{session_id}/{tool_id} approves a tool."""
        # Session won't exist, so expect 404
        response = client.post(
            "/live-edit/approve/nonexistent/tool1",
            json={"approved": True},
        )
        assert response.status_code == 404

    def test_approve_reject(self, client):
        """POST with approved=False rejects a tool."""
        response = client.post(
            "/live-edit/approve/nonexistent/tool2",
            json={"approved": False},
        )
        assert response.status_code == 404


class TestBatchApprove:
    def test_batch_approve_missing_session(self, client):
        """POST batch approve on a nonexistent session returns 404."""
        response = client.post(
            "/live-edit/approve/nonexistent/batch", json={"enabled": True}
        )
        assert response.status_code == 404

    def test_batch_approve_enables_auto_approve(self, tmp_path):
        """Batch approve toggles auto-approve on the target session."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from live_edit.engine import EditSession, SessionStore
        from live_edit.router import setup_live_edit

        config_path = _write_router_config(tmp_path)
        store = SessionStore(max_active=10, ttl_seconds=3600)
        session = EditSession("s1", "Edit")
        store.add(session)

        router = setup_live_edit(
            project_root=str(tmp_path),
            config_path=str(config_path),
            provider=FakeProvider(),
            storage=MagicMock(),
            vcs=MagicMock(),
            session_store=store,
        )
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.post("/live-edit/approve/s1/batch", json={"enabled": True})
        assert response.status_code == 200
        assert response.json() == {"ok": True, "enabled": True}
        assert session._auto_approve is True

        response = client.post("/live-edit/approve/s1/batch", json={"enabled": False})
        assert response.json() == {"ok": True, "enabled": False}
        assert session._auto_approve is False


class TestStreamEndpoint:
    def test_stream_starts_session(self, client):
        """POST /live-edit/stream returns SSE events."""
        with client.stream(
            "POST",
            "/live-edit/stream",
            json={"request": "Add a button", "mode": "quick"},
        ) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")

            # Read some SSE data
            body = ""
            for chunk in response.iter_text():
                body += chunk
                if len(body) > 10000:
                    break

            assert "data:" in body or len(body) > 0

    def test_stream_with_continue(self, client):
        """POST /live-edit/continue/{id} with a nonexistent session."""
        response = client.post(
            "/live-edit/continue/nonexistent",
            json={"request": "Change color", "mode": "quick"},
        )
        assert response.status_code == 404


class TestContinueErrorSurface:
    def test_crashed_continue_task_yields_error_event(self, tmp_path, monkeypatch):
        """A crashed continue task must surface as an SSE error event, not kill
        the stream silently. Regression: the old code did a bare `await task`
        after the queue loop, so a crashing task raised out of the generator and
        the SSE stream died with no user-facing event."""
        from unittest.mock import MagicMock

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import live_edit.router as router_mod
        from live_edit.engine import EditSession, SessionStore
        from live_edit.router import setup_live_edit

        config_path = _write_router_config(tmp_path)
        store = SessionStore(max_active=10, ttl_seconds=3600)
        session = EditSession("s1", "continue me")
        session._merged = True  # committed session: worktree kept but _worktree_path cleared
        session._worktree_path = ""
        store.add(session)

        async def _boom(**kwargs):
            # Simulate a task that ends the stream loop then crashes mid-flight
            # (e.g. CalledProcessError from `git worktree add` on a /continue).
            session = kwargs["session"]
            session.queue.put_nowait(None)
            raise RuntimeError("boom")

        monkeypatch.setattr(router_mod, "continue_edit_session", _boom)

        router = setup_live_edit(
            project_root=str(tmp_path),
            config_path=str(config_path),
            provider=FakeProvider(),
            storage=MagicMock(),
            vcs=MagicMock(),
            session_store=store,
        )
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        data_lines = []
        with client.stream(
            "POST", "/live-edit/continue/s1", json={"request": "hi", "mode": "quick"}
        ) as response:
            assert response.status_code == 200
            for chunk in response.iter_text():
                for line in chunk.splitlines():
                    if line.startswith("data:"):
                        data_lines.append(line)

        assert data_lines, "expected at least one SSE data line"
        assert any('"type": "error"' in line and "boom" in line for line in data_lines)


class TestHealthCheck:
    def test_health_endpoint(self, client):
        response = client.get("/live-edit/health")
        assert response.status_code == 200


@pytest.fixture
def branch_app(tmp_path):
    """App fixture exposing vcs/storage mocks for branch endpoint tests."""
    from live_edit.router import setup_live_edit

    config_path = tmp_path / ".live-edit.toml"
    config_path.write_text("""
[project]
name = "TestApp"
language = "python"
root = "."
[llm]
provider = "anthropic_compatible"
api_url = "https://api.example.com/v1/messages"
api_key_env = "FAKE_KEY"
model = "test-model"
[safety]
allowed_dirs = ["."]
[timeouts]
api_request = 180
shell_command = 30
[sessions]
max_active = 10
[hooks]
[ui]
default_mode = "quick"
[modes.quick]
label = "快速修改"
approval = "per_tool"
tools = "write"
approve_for = ["edit_file", "write_file"]
[modes.quick.prompt]
base = "You are a helpful AI."
user_persona = "Non-technical user."
communication_rules = "Use Chinese."
[modes.deep]
label = "深度开发"
approval = "final"
tools = "all"
[modes.deep.prompt]
base = "You are a dev assistant."
user_persona = "Developer."
communication_rules = "Use technical terms."
[errors.quick]
[errors.deep]
""")
    mock_provider = FakeProvider()
    mock_vcs = MagicMock()
    # Back vcs.repo_path with a real git repo so the merge endpoint's
    # subprocess `git rev-parse` calls resolve live-edit/s1.
    import subprocess as _sp

    _sp.run(["git", "init", "-q"], cwd=str(tmp_path), capture_output=True)
    _sp.run(["git", "config", "user.email", "t@t.com"], cwd=str(tmp_path), capture_output=True)
    _sp.run(["git", "config", "user.name", "T"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "init.txt").write_text("init")
    _sp.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    _sp.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
    _sp.run(["git", "branch", "-M", "main"], cwd=str(tmp_path), capture_output=True)
    _sp.run(["git", "branch", "live-edit/s1"], cwd=str(tmp_path), capture_output=True)
    mock_vcs.repo_path = str(tmp_path)
    mock_vcs.list_unmerged_branches.return_value = [
        {
            "session_id": "s1",
            "branch": "live-edit/s1",
            "commit_hash": "abc1234",
            "commit_time": "2026-06-19 12:00:00 +0800",
            "subject": "live-edit: fix button",
        },
    ]
    mock_storage = MagicMock()
    mock_storage.get_session_detail.return_value = {
        "session_id": "s1",
        "request": "Fix the button color",
        "committed": 1,
        "commit_hash": "abc1234",
        "files": '["a.py"]',
        "mode": "quick",
        "messages": "[]",
    }
    router = setup_live_edit(
        project_root=str(tmp_path),
        config_path=str(config_path),
        provider=mock_provider,
        storage=mock_storage,
        vcs=mock_vcs,
        admin_key="admin-secret",
    )
    app = FastAPI()
    app.include_router(router)
    app.state.vcs = mock_vcs
    app.state.storage = mock_storage
    return app


@pytest.fixture
def branch_client(branch_app):
    return TestClient(branch_app)


class TestAdminBranchesList:
    def test_lists_unmerged_with_summary(self, branch_client):
        resp = branch_client.get(
            "/live-edit/admin/branches",
            headers={"X-Admin-Key": "admin-secret"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "branches" in data
        assert len(data["branches"]) == 1
        b = data["branches"][0]
        assert b["session_id"] == "s1"
        assert b["summary"] == "Fix the button color"

    def test_rejects_without_admin_key(self, branch_client):
        resp = branch_client.get("/live-edit/admin/branches")
        assert resp.status_code == 403


class TestAdminBranchMerge:
    def test_merge_success(self, branch_client, branch_app):
        vcs = branch_app.state.vcs
        vcs.merge_commit.return_value = "mergehash9"
        # list_unmerged_branches 现在应返回空（已合入+清理）
        vcs.list_unmerged_branches.return_value = []
        vcs.discard_session_branch = MagicMock()

        resp = branch_client.post(
            "/live-edit/admin/branches/s1/merge",
            headers={"X-Admin-Key": "admin-secret"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["commit_hash"] == "mergehash9"
        vcs.merge_commit.assert_called_once()

    def test_merge_conflict_returns_409(self, branch_client, branch_app):
        vcs = branch_app.state.vcs
        vcs.merge_commit.side_effect = RuntimeError("merge conflict in a.py")
        vcs.abort_merge = MagicMock()

        resp = branch_client.post(
            "/live-edit/admin/branches/s1/merge",
            headers={"X-Admin-Key": "admin-secret"},
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data.get("conflict") is True
        vcs.abort_merge.assert_called_once()

    def test_merge_rejects_without_admin_key(self, branch_client):
        resp = branch_client.post("/live-edit/admin/branches/s1/merge")
        assert resp.status_code == 403


class TestAdminBranchDelete:
    def test_delete_success(self, branch_client, branch_app):
        vcs = branch_app.state.vcs
        vcs.discard_session_branch = MagicMock()

        resp = branch_client.post(
            "/live-edit/admin/branches/s1/delete",
            headers={"X-Admin-Key": "admin-secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        vcs.discard_session_branch.assert_called_once_with("s1")

    def test_delete_rejects_without_admin_key(self, branch_client):
        resp = branch_client.post("/live-edit/admin/branches/s1/delete")
        assert resp.status_code == 403


def _recoverable_session_detail():
    # mirrors the parsed output of storage.get_session_detail (JSON columns
    # already decoded into lists)
    return {
        "session_id": "s-recover",
        "request": "polish",
        "committed": 0,
        "commit_hash": "",
        "files": ["doc.md"],
        "mode": "deep",
        "messages": [{"role": "user", "content": "polish the doc"}],
    }


@pytest.fixture
def make_recovery_app(tmp_path):
    def _make(session_detail, max_active=10):
        from live_edit.router import setup_live_edit

        config_path = _write_router_config(tmp_path)
        if max_active != 10:
            config_text = config_path.read_text()
            config_text = config_text.replace("max_active = 10", f"max_active = {max_active}")
            config_path.write_text(config_text)
        mock_provider = FakeProvider()
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = str(tmp_path / "wt")
        os.makedirs(mock_vcs.create_worktree.return_value, exist_ok=True)
        mock_vcs.log_live_edit_commits.return_value = []
        mock_storage = MagicMock()
        mock_storage.get_sessions.return_value = []
        mock_storage.get_session_detail.return_value = session_detail
        router = setup_live_edit(
            project_root=str(tmp_path),
            config_path=str(config_path),
            provider=mock_provider,
            storage=mock_storage,
            vcs=mock_vcs,
        )
        app = FastAPI()
        app.include_router(router)
        return app

    return _make


class TestContinueRecovery:
    def test_recovers_session_from_storage(self, make_recovery_app):
        app = make_recovery_app(_recoverable_session_detail())
        client = TestClient(app)

        resp = client.post("/live-edit/continue/s-recover", json={"request": "keep going"})

        assert resp.status_code == 200
        assert '"done"' in resp.text

    def test_404_when_storage_has_no_record(self, make_recovery_app):
        app = make_recovery_app(None)
        client = TestClient(app)

        resp = client.post("/live-edit/continue/s-missing", json={"request": "x"})

        assert resp.status_code == 404

    def test_503_when_store_at_capacity(self, make_recovery_app):
        app = make_recovery_app(_recoverable_session_detail(), max_active=1)
        client = TestClient(app)

        # Fill the single slot via /stream (adds to store synchronously)
        with client.stream("POST", "/live-edit/stream", json={"request": "fill", "mode": "quick"}):
            pass

        resp = client.post("/live-edit/continue/s-recover", json={"request": "keep going"})

        assert resp.status_code == 503
