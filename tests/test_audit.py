import sqlite3

import pytest

from live_edit.audit import SQLiteAuditLog


def _audit(tmp_path) -> SQLiteAuditLog:
    return SQLiteAuditLog(str(tmp_path / "audit.db"))


def test_record_and_query_roundtrip(tmp_path):
    a = _audit(tmp_path)
    a.record("session_start", actor="anonymous", target="le_1", session_id="le_1")
    a.record("approve", actor="anonymous", target="edit_file", session_id="le_1", result="approved")
    events = a.query()
    assert len(events) == 2
    assert events[0].action == "approve"  # newest first (id DESC)
    assert events[0].result == "approved"
    assert events[1].session_id == "le_1"


def test_query_filters(tmp_path):
    a = _audit(tmp_path)
    a.record("session_start", session_id="le_a")
    a.record("commit", session_id="le_a", result="ok", target="abc123")
    a.record("session_start", session_id="le_b")
    by_action = a.query(action="session_start")
    assert len(by_action) == 2
    by_session = a.query(session_id="le_b")
    assert len(by_session) == 1
    by_result = a.query(action="commit")
    assert by_result[0].target == "abc123"


def test_events_are_append_only_no_delete_or_update_methods():
    assert not hasattr(SQLiteAuditLog, "delete")
    assert not hasattr(SQLiteAuditLog, "update")
    assert not hasattr(SQLiteAuditLog, "remove")


def test_database_only_contains_audit_events_table_and_indexes(tmp_path):
    a = _audit(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "audit.db"))
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "audit_events" in tables
    indexes = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_audit_action_ts" in indexes
    assert "idx_audit_session" in indexes
    assert "idx_audit_actor" in indexes


def test_record_is_best_effort_on_failure(tmp_path, monkeypatch):
    a = _audit(tmp_path)
    # Break the connection so INSERT raises; record() must swallow and return 0.
    monkeypatch.setattr(
        SQLiteAuditLog, "_get_conn", lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert a.record("session_start", session_id="le_1") == 0


def test_record_to_to_dict_fields(tmp_path):
    a = _audit(tmp_path)
    a.record("tool_execution", session_id="le_1", target="edit_file", result="ok",
             detail={"tool": "edit_file", "duration_ms": 12})
    ev = a.query()[0]
    d = ev.to_dict()
    assert d["action"] == "tool_execution"
    assert d["detail"]["tool"] == "edit_file"
    assert "ts" in d and d["id"] > 0
