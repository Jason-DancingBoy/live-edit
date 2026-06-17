"""Tests for live_edit.storage — Storage interface and SQLiteStorage."""

import json
import time
import pytest
from live_edit.storage import SQLiteStorage


class TestSQLiteStorage:
    @pytest.fixture
    def storage(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        return SQLiteStorage(db_path)

    def test_init_creates_tables(self, storage):
        conn = storage._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert "live_edit_sessions" in table_names

    def test_save_and_retrieve_session(self, storage):
        storage.save_session(
            session_id="abc123",
            request="Make the button red",
            committed=True,
            files=["index.html", "style.css"],
            commit_hash="a1b2c3d",
            messages_json=json.dumps([{"role": "user", "content": "test"}]),
            mode="quick",
        )

        sessions = storage.get_sessions(limit=10)
        assert len(sessions) == 1
        s = sessions[0]
        assert s["session_id"] == "abc123"
        assert s["request"] == "Make the button red"
        assert s["committed"] == 1
        assert s["commit_hash"] == "a1b2c3d"
        assert s["mode"] == "quick"

    def test_get_session_detail(self, storage):
        messages = [{"role": "user", "content": "test message"}]
        storage.save_session(
            session_id="detail1",
            request="Test",
            committed=False,
            files=["app.py"],
            commit_hash="",
            messages_json=json.dumps(messages, ensure_ascii=False),
            mode="deep",
        )

        detail = storage.get_session_detail("detail1")
        assert detail is not None
        assert detail["session_id"] == "detail1"
        assert detail["mode"] == "deep"
        parsed = json.loads(detail["messages"])
        assert parsed[0]["content"] == "test message"

    def test_get_nonexistent_session(self, storage):
        assert storage.get_session_detail("nonexistent") is None

    def test_get_sessions_limit(self, storage):
        for i in range(15):
            storage.save_session(
                session_id=f"s{i}",
                request=f"Request {i}",
                committed=False,
                files=[],
                commit_hash="",
                messages_json="[]",
                mode="quick",
            )

        sessions = storage.get_sessions(limit=5)
        assert len(sessions) == 5

    def test_sessions_ordered_by_created_at_desc(self, storage):
        for i in range(3):
            storage.save_session(
                session_id=f"s{i}",
                request=f"Request {i}",
                committed=False,
                files=[],
                commit_hash="",
                messages_json="[]",
                mode="quick",
            )
            time.sleep(1.1)  # SQLite datetime('now') is second-granularity

        sessions = storage.get_sessions(limit=10)
        # Most recent first
        assert sessions[0]["request"] == "Request 2"
        assert sessions[2]["request"] == "Request 0"
        assert sessions[-1]["request"] == "Request 0"
