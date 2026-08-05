# tests/test_session_store_audit.py
from live_edit.audit import SQLiteAuditLog
from live_edit.engine import EditSession, SessionStore


def test_session_expired_is_audited_on_ttl(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "audit.db"))
    store = SessionStore(max_active=10, ttl_seconds=0, audit_log=audit)
    sess = EditSession("le_1", "request")
    store.add(sess)
    assert store.get("le_1") is None  # TTL=0 forces expiry on read
    events = audit.query(action="session_expired")
    assert len(events) == 1
    assert events[0].session_id == "le_1"


def test_session_expired_audited_by_expire_stale(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "audit.db"))
    store = SessionStore(max_active=10, ttl_seconds=0, audit_log=audit)
    sess = EditSession("le_2", "request")
    store.add(sess)
    assert store.count == 0  # _expire_stale triggered and removed the expired session
    events = audit.query(action="session_expired")
    assert len(events) == 1
    assert events[0].session_id == "le_2"
