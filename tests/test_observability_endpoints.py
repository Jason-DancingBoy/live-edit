# tests/test_observability_endpoints.py
from fastapi import FastAPI
from fastapi.testclient import TestClient

from live_edit.audit import SQLiteAuditLog
from live_edit.metrics import Metrics
from live_edit.router import setup_live_edit


def _write_config(tmp_path, audit_enabled: bool = True):
    cfg = tmp_path / ".live-edit.toml"
    cfg.write_text(
        f"""[project]
name = "TestApp"
language = "python"
root = "."

[llm]
provider = "anthropic_compatible"
api_url = "https://api.example.com/v1/messages"
api_key_env = "FAKE_KEY"
model = "test-model"

[sessions]
max_active = 10

[modes.quick]
label = "快速修改"
approval = "per_tool"
tools = "write"

[modes.quick.prompt]
base = "You are a helpful AI."

[observability]
log_level = "INFO"
json_logs = true
metrics_enabled = true
audit_enabled = {str(audit_enabled).lower()}
"""
    )
    return str(cfg)


def _client(tmp_path, audit_enabled: bool = True, **kwargs):
    _write_config(
        tmp_path, audit_enabled=audit_enabled
    )  # config file must exist: parse_config raises FileNotFoundError otherwise
    app = FastAPI()
    router = setup_live_edit(
        project_root=str(tmp_path),
        config_path=".live-edit.toml",
        admin_key="secret-admin",
        **kwargs,
    )
    app.include_router(router)
    return TestClient(app)


def test_metrics_endpoint_returns_prometheus_text(tmp_path):
    metrics = Metrics()
    metrics.inc("live_edit_sessions_total", {"outcome": "started"})
    client = _client(tmp_path, metrics=metrics)
    resp = client.get("/live-edit/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert 'live_edit_sessions_total{outcome="started"} 1' in resp.text


def test_admin_audit_requires_admin_key(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "live_edit.db"))
    audit.record("session_start", session_id="le_1")
    client = _client(tmp_path, audit_log=audit)
    resp = client.get("/live-edit/admin/audit")
    assert resp.status_code == 403
    ok = client.get("/live-edit/admin/audit", headers={"X-Admin-Key": "secret-admin"})
    assert ok.status_code == 200
    events = ok.json()["events"]
    # The preceding no-key 403 also records a failed_admin_auth event, so the
    # returned list contains both it and the original session_start record.
    assert any(e["action"] == "session_start" for e in events)


def test_failed_admin_auth_is_audited(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "live_edit.db"))
    client = _client(tmp_path, audit_log=audit)
    client.get("/live-edit/admin/audit", headers={"X-Admin-Key": "wrong"})
    events = audit.query(action="failed_admin_auth")
    assert len(events) == 1
    assert events[0].result == "blocked"


def test_admin_audit_supports_filters(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "live_edit.db"))
    audit.record("session_start", session_id="le_a")
    audit.record("commit", session_id="le_a", result="ok", target="c1")
    client = _client(tmp_path, audit_log=audit)
    resp = client.get(
        "/live-edit/admin/audit",
        headers={"X-Admin-Key": "secret-admin"},
        params={"action": "commit"},
    )
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["target"] == "c1"


def test_stream_audits_session_start_and_metrics(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "live_edit.db"))
    metrics = Metrics()

    # Inject fast fakes so the SSE stream completes instead of blocking: the real
    # GitVCS raises on tmp_path (not a git repo) inside engine's run_edit_session
    # BEFORE its try block, so the queue never gets None and the stream hangs for
    # the full 180s queue timeout; the real LLM provider would also retry real
    # network calls. The wiring under test (session_start audit + started metric)
    # lives in the router's start_stream, which runs before the engine anyway.
    from unittest.mock import MagicMock

    class FakeProvider:
        async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
            return [{"type": "text", "text": "Done."}]

    mock_vcs = MagicMock()
    mock_vcs.create_worktree.return_value = str(tmp_path)
    client = _client(
        tmp_path,
        audit_log=audit,
        metrics=metrics,
        provider=FakeProvider(),
        vcs=mock_vcs,
    )
    resp = client.post(
        "/live-edit/stream",
        json={"request": "hello", "mode": "quick"},
    )
    # The SSE stream is drained; at minimum a session_start audit exists.
    assert resp.status_code == 200
    started = audit.query(action="session_start")
    assert len(started) == 1
    assert "live_edit_sessions_total{outcome=\"started\"}" in metrics.render()


def test_correlation_id_header_is_echoed(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/live-edit/health", headers={"X-Request-ID": "req-custom"})
    assert resp.headers.get("X-Request-ID") == "req-custom"


def test_audit_disabled_does_not_crash(tmp_path):
    """audit_enabled=false with no injected audit_log must not break the app.

    The no-op NullAuditLog keeps record/query safe, so health, the SSE stream,
    and the admin audit endpoint all still work.
    """
    from unittest.mock import MagicMock

    class FakeProvider:
        async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
            return [{"type": "text", "text": "Done."}]

    mock_vcs = MagicMock()
    mock_vcs.create_worktree.return_value = str(tmp_path)
    client = _client(
        tmp_path,
        audit_enabled=False,
        provider=FakeProvider(),
        vcs=mock_vcs,
    )
    health = client.get("/live-edit/health")
    assert health.status_code == 200
    stream = client.post("/live-edit/stream", json={"request": "hello", "mode": "quick"})
    assert stream.status_code == 200
    audit = client.get("/live-edit/admin/audit", headers={"X-Admin-Key": "secret-admin"})
    assert audit.status_code == 200
    assert audit.json() == {"events": []}
