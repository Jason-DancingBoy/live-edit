"""Tests for live_edit.storage — Storage interface and SQLiteStorage."""

import json
import struct
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


class TestSessionEmbeddings:
    @pytest.fixture
    def storage(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        return SQLiteStorage(db_path)

    def test_init_creates_embeddings_table(self, storage):
        conn = storage._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert "session_embeddings" in table_names

    def test_store_and_query_embedding(self, storage):
        emb = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
        storage.store_embedding(
            session_id="s1",
            request="Make button red",
            files_json=json.dumps(["a.py", "b.css"], ensure_ascii=False),
            embedding=emb,
        )
        rows = storage.query_embeddings()
        assert len(rows) == 1
        assert rows[0][0] == "s1"
        assert rows[0][1] == "Make button red"
        assert rows[0][3] == emb

    def test_store_replace_updates_existing(self, storage):
        emb1 = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
        emb2 = struct.pack("4f", 0.5, 0.6, 0.7, 0.8)
        storage.store_embedding("s1", "first", "[]", emb1)
        storage.store_embedding("s1", "updated", '["x.py"]', emb2)
        rows = storage.query_embeddings()
        assert len(rows) == 1
        assert rows[0][1] == "updated"
        assert rows[0][3] == emb2

    def test_query_returns_empty_list_when_no_data(self, storage):
        assert storage.query_embeddings() == []

    def test_delete_old_embeddings_keeps_most_recent(self, storage):
        for i in range(5):
            emb = struct.pack("4f", float(i), 0.0, 0.0, 0.0)
            storage.store_embedding(f"s{i}", f"req{i}", "[]", emb)
            time.sleep(1.1)
        storage.delete_old_embeddings(keep_count=3)
        rows = storage.query_embeddings()
        assert len(rows) == 3
        assert rows[0][1] == "req4"
