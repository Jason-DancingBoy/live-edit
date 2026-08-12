"""Test that /live-edit/continue/{session_id} emits the initial session ack.

For parity with /live-edit/stream, the /continue SSE stream must emit a
`session` event as its first data block so clients get immediate feedback
instead of silence until the model produces its first output.
"""

import json
import re
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from live_edit.audit import SQLiteAuditLog
from live_edit.storage import SQLiteStorage
from live_edit.vcs import GitVCS


class FakeProvider:
    def __init__(self):
        self.call_count = 0

    async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
        self.call_count += 1
        return [{"type": "text", "text": "完成。"}]


class RecordingGitVCS(GitVCS):
    """Real GitVCS that records the (session_id, base_ref) it was asked to fork from."""

    def __init__(self, repo: str):
        super().__init__(repo)
        self.fork_calls: list[tuple[str, str]] = []

    def create_worktree(self, session_id: str, base_ref: str = "") -> str:
        self.fork_calls.append((session_id, base_ref))
        return super().create_worktree(session_id, base_ref=base_ref)


def _write_router_config(tmp_path):
    """Minimal .live-edit.toml (same shape the existing router tests use)."""
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
[errors.deep]
"""
    )
    return config_path


def _init_repo(tmp_path):
    """Init a real git repo with one commit on main; returns (repo_path, head_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
    (repo / "init.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo), capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()
    return str(repo), head


def _seed_session(storage, session_id, committed, commit_hash, base_session_id=""):
    storage.save_session(
        session_id=session_id,
        request="seed",
        committed=committed,
        files=["init.txt"],
        commit_hash=commit_hash,
        messages_json="[]",
        mode="quick",
        base_session_id=base_session_id,
    )


@pytest.fixture
def fork_app(tmp_path):
    """App with real git + real sqlite + real audit; only the provider is faked."""
    from live_edit.router import setup_live_edit

    repo, head = _init_repo(tmp_path)
    config_path = _write_router_config(tmp_path)
    db_path = str(tmp_path / "live_edit.db")

    storage = SQLiteStorage(db_path)
    audit = SQLiteAuditLog(db_path)
    vcs = RecordingGitVCS(repo)
    _seed_session(storage, "s1", committed=True, commit_hash=head)

    router = setup_live_edit(
        project_root=str(tmp_path),
        config_path=str(config_path),
        provider=FakeProvider(),
        storage=storage,
        vcs=vcs,
        audit_log=audit,
    )
    app = FastAPI()
    app.include_router(router)
    app.state.storage = storage
    app.state.audit = audit
    app.state.vcs = vcs
    return app


def _run_stream(client, payload):
    """POST /live-edit/stream and read the SSE body to completion."""
    with client.stream("POST", "/live-edit/stream", json=payload) as resp:
        assert resp.status_code == 200
        return "".join(resp.iter_text())


def _extract_session_id(sse_body: str) -> str:
    m = re.search(r'"type": "session", "session_id": "([^"]+)"', sse_body)
    assert m, f"no session event in SSE body: {sse_body[:300]}"
    return m.group(1)


class TestContinueAck:
    def test_continue_emits_session_ack_first(self, fork_app):
        client = TestClient(fork_app)
        body = _run_stream(client, {"request": "add", "mode": "quick"})
        sid = _extract_session_id(body)

        with client.stream(
            "POST", f"/live-edit/continue/{sid}", json={"request": "more", "mode": "quick"}
        ) as resp:
            assert resp.status_code == 200
            continue_body = "".join(resp.iter_text())

        data_blocks = [
            b[len("data: ") :] for b in continue_body.split("\n\n") if b.startswith("data: ")
        ]
        assert data_blocks, "expected at least one SSE data block"
        first = json.loads(data_blocks[0])
        assert first["type"] == "session"
        assert first["session_id"] == sid
