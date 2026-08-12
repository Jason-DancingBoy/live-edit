"""Evidence-aware admin merge gate + session-detail evidence tests."""

import json
import subprocess
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from live_edit.audit import SQLiteAuditLog
from live_edit.router import setup_live_edit
from live_edit.storage import SQLiteStorage


def _write_config(tmp_path) -> str:
    """Write a minimal .live-edit.toml and return its absolute path.

    setup_live_edit 会 open(config_path)（parse_config），文件缺失会抛
    FileNotFoundError，所以必须先写配置文件。
    """
    config_path = tmp_path / ".live-edit.toml"
    config_path.write_text(
        """[project]
name = "t"
language = "python"
root = "."

[llm]
provider = "anthropic_compatible"
api_url = "http://x"
api_key_env = "K"
model = "m"
"""
    )
    return str(config_path)


def _make_app(tmp_path, vcs, audit_log=None):
    """Build a router backed by a real SQLiteStorage and the given vcs."""
    storage = SQLiteStorage(str(tmp_path / "s.db"))
    router = setup_live_edit(
        project_root=str(tmp_path),
        config_path=_write_config(tmp_path),
        storage=storage,
        vcs=vcs,
        admin_key="k",
        audit_log=audit_log,
    )
    app = FastAPI()
    app.include_router(router)
    return app, storage


def _make_git_repo(tmp_path, sid):
    """Real temp git repo with a live-edit/<sid> branch + MagicMock vcs wired to it.

    The merge endpoint runs `git rev-parse --verify live-edit/<sid>` against
    vcs.repo_path BEFORE merging, so the repo must actually have the branch
    (otherwise the merge-succeeds tests 404).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
    (repo / "init.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "checkout", "-b", f"live-edit/{sid}"], cwd=str(repo), capture_output=True
    )
    vcs = MagicMock()
    vcs.repo_path = str(repo)
    vcs.merge_commit.return_value = "m1"
    vcs.discard_session_branch = MagicMock()
    return vcs


def _store_evidence(storage, session_id, decision):
    storage.save_evidence(
        session_id,
        json.dumps({"session_id": session_id, "decision": decision, "layers": {}}),
    )


def test_merge_blocked_requires_reason(tmp_path):
    app, storage = _make_app(tmp_path, vcs=MagicMock())
    _store_evidence(storage, "s1", "block")
    r = TestClient(app).post(
        "/live-edit/admin/branches/s1/merge", headers={"X-Admin-Key": "k"}
    )
    assert r.status_code == 400
    assert r.json().get("blocked") is True


def test_merge_blocked_whitespace_reason_still_blocked_and_audited(tmp_path):
    """纯空白 reason 不构成强制放行理由：仍 400，且留下 admin_merge_blocked 审计。"""
    audit = SQLiteAuditLog(str(tmp_path / "audit.db"))
    app, storage = _make_app(tmp_path, vcs=MagicMock(), audit_log=audit)
    _store_evidence(storage, "s1", "block")
    r = TestClient(app).post(
        "/live-edit/admin/branches/s1/merge",
        headers={"X-Admin-Key": "k"},
        json={"reason": "   "},
    )
    assert r.status_code == 400
    assert r.json().get("blocked") is True
    blocked = audit.query(action="admin_merge_blocked")
    assert len(blocked) == 1
    assert blocked[0].result == "blocked"
    assert blocked[0].detail == {"reason": "   "}


def test_merge_blocked_with_reason_overrides(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "audit.db"))
    vcs = _make_git_repo(tmp_path, "s1")
    app, storage = _make_app(tmp_path, vcs=vcs, audit_log=audit)
    _store_evidence(storage, "s1", "block")
    r = TestClient(app).post(
        "/live-edit/admin/branches/s1/merge",
        headers={"X-Admin-Key": "k"},
        json={"reason": "人工确认过"},
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "block"
    assert r.json()["commit_hash"] == "m1"

    overrides = audit.query(action="admin_merge_override")
    assert len(overrides) == 1
    assert overrides[0].result == "ok"
    assert overrides[0].detail == {"reason": "人工确认过"}
    merges = audit.query(action="admin_merge")
    assert len(merges) == 1
    assert merges[0].result == "override"


def test_merge_auto_approve_merges(tmp_path):
    vcs = _make_git_repo(tmp_path, "s1")
    app, storage = _make_app(tmp_path, vcs=vcs)
    _store_evidence(storage, "s1", "auto_approve")
    r = TestClient(app).post(
        "/live-edit/admin/branches/s1/merge", headers={"X-Admin-Key": "k"}
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "auto_approve"


def test_session_detail_includes_evidence(tmp_path):
    app, storage = _make_app(tmp_path, vcs=MagicMock())
    storage.save_session(
        session_id="s1",
        request="改按钮",
        committed=0,
        files=["app.py"],
        commit_hash="",
        messages_json="[]",
        mode="quick",
    )
    _store_evidence(storage, "s1", "auto_approve")
    r = TestClient(app).get("/live-edit/session/s1")
    assert r.status_code == 200
    assert r.json().get("evidence", {}).get("decision") == "auto_approve"


def test_session_detail_corrupted_evidence_does_not_500(tmp_path):
    """损坏的 evidence 字符串不应让会话详情端 500，按无证据处理。"""
    app, storage = _make_app(tmp_path, vcs=MagicMock())
    storage.save_session(
        session_id="s1",
        request="改按钮",
        committed=0,
        files=["app.py"],
        commit_hash="",
        messages_json="[]",
        mode="quick",
    )
    storage.save_evidence("s1", "{not valid json")
    r = TestClient(app).get("/live-edit/session/s1")
    assert r.status_code == 200
    assert r.json().get("evidence") is None
