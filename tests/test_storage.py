"""Tests for live_edit.storage — Storage interface and SQLiteStorage."""

import json
import json as _json
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
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
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
        assert isinstance(detail["messages"], list)
        assert detail["messages"][0]["content"] == "test message"

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
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
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


class TestSessionChunks:
    @pytest.fixture
    def storage(self, tmp_path):
        db_path = str(tmp_path / "test_chunks.db")
        return SQLiteStorage(db_path)

    def test_init_creates_chunks_table(self, storage):
        conn = storage._get_conn()
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t[0] for t in tables]
        assert "session_chunks" in table_names

    def test_store_and_query_chunks(self, storage):
        emb = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
        chunks = [
            {
                "chunk_type": "request",
                "chunk_text": "add login",
                "payload_json": _json.dumps({"request": "add login"}),
                "file_path": "",
                "embedding_bytes": emb,
            },
            {
                "chunk_type": "file_diff",
                "chunk_text": "add login\nFile: auth.py\nChanges: +10/-2",
                "payload_json": _json.dumps({"file": "auth.py", "diff": "+import jwt"}),
                "file_path": "auth.py",
                "embedding_bytes": emb,
            },
        ]
        storage.store_chunks("s1", "abc123", chunks)
        rows = storage.query_chunks()
        assert len(rows) == 2
        assert rows[0][3] in ("request", "file_diff")  # chunk_type at index 3

    def test_store_replaces_existing_session(self, storage):
        emb = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
        chunks1 = [
            {
                "chunk_type": "request",
                "chunk_text": "first",
                "payload_json": "{}",
                "file_path": "",
                "embedding_bytes": emb,
            }
        ]
        chunks2 = [
            {
                "chunk_type": "request",
                "chunk_text": "second",
                "payload_json": "{}",
                "file_path": "",
                "embedding_bytes": emb,
            }
        ]
        storage.store_chunks("s1", "abc", chunks1)
        storage.store_chunks("s1", "def", chunks2)
        rows = storage.query_chunks()
        assert len(rows) == 1
        assert rows[0][4] == "second"  # chunk_text at index 4

    def test_delete_session_chunks(self, storage):
        emb = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
        chunks = [
            {
                "chunk_type": "request",
                "chunk_text": "test",
                "payload_json": "{}",
                "file_path": "",
                "embedding_bytes": emb,
            }
        ]
        storage.store_chunks("s1", "abc", chunks)
        storage.delete_session_chunks("s1")
        assert storage.query_chunks() == []

    def test_delete_old_sessions_keeps_most_recent(self, storage):
        import time

        emb = struct.pack("4f", 0.0, 0.0, 0.0, 0.0)
        for i in range(5):
            chunks = [
                {
                    "chunk_type": "request",
                    "chunk_text": f"req{i}",
                    "payload_json": "{}",
                    "file_path": "",
                    "embedding_bytes": emb,
                }
            ]
            storage.store_chunks(f"s{i}", "hash", chunks)
            time.sleep(1.1)  # ensure distinct created_at timestamps

        storage.delete_old_sessions(keep_count=3)
        rows = storage.query_chunks()
        session_ids = {r[1] for r in rows}  # session_id at index 1
        assert len(session_ids) == 3
        # Most recent sessions survive
        assert "s4" in session_ids
        assert "s0" not in session_ids

    def test_store_chunks_rolls_back_on_error(self, storage):
        """If one INSERT fails, all DELETEs are rolled back."""
        emb = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
        # Pre-populate
        storage.store_chunks(
            "s1",
            "abc",
            [
                {
                    "chunk_type": "request",
                    "chunk_text": "original",
                    "payload_json": "{}",
                    "file_path": "",
                    "embedding_bytes": emb,
                }
            ],
        )
        # Try to store a chunk missing required key — should fail
        bad_chunks = [{"not_a_valid_chunk": True}]
        with pytest.raises(Exception):  # noqa: B017
            storage.store_chunks("s1", "def", bad_chunks)
        # Original data should still be intact
        rows = storage.query_chunks()
        assert len(rows) == 1
        assert rows[0][4] == "original"

    def test_db_version_migrates_to_v2_on_init(self, storage):
        # Fresh DB is auto-migrated to schema v2 during __init__
        assert storage.get_db_version() == 2

    def test_set_and_get_db_version(self, storage):
        storage.set_db_version(1)
        assert storage.get_db_version() == 1

    def test_query_chunks_respects_limit(self, storage):
        emb = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
        for i in range(5):
            chunks = [
                {
                    "chunk_type": "request",
                    "chunk_text": f"req{i}",
                    "payload_json": "{}",
                    "file_path": "",
                    "embedding_bytes": emb,
                }
            ]
            storage.store_chunks(f"s{i}", "hash", chunks)
        rows = storage.query_chunks(limit=3)
        assert len(rows) == 3


class TestMigrationV2:
    def test_adds_hit_columns_to_session_chunks(self, tmp_path):
        from live_edit.storage import SQLiteStorage

        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))
        conn = store._get_conn()

        # Verify columns exist after migration
        cols = [r[1] for r in conn.execute("PRAGMA table_info(session_chunks)")]
        assert "hit_count" in cols
        assert "last_accessed" in cols

    def test_migration_idempotent(self, tmp_path):
        from live_edit.storage import SQLiteStorage

        db = tmp_path / "test.db"
        store1 = SQLiteStorage(str(db))
        conn1 = store1._get_conn()
        version1 = conn1.execute("PRAGMA user_version").fetchone()[0]

        # Re-open and re-migrate
        store2 = SQLiteStorage(str(db))
        conn2 = store2._get_conn()
        version2 = conn2.execute("PRAGMA user_version").fetchone()[0]

        assert version1 == version2

    def test_backfills_vec_table(self, tmp_path):
        # Requires sqlite-vec (installed via the dev/rag extras); skip otherwise.
        pytest.importorskip("sqlite_vec")
        from live_edit.storage import SQLiteStorage

        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))

        # Insert a chunk manually
        conn = store._get_conn()
        import struct

        emb = struct.pack("384f", *[0.1] * 384)
        conn.execute(
            """INSERT INTO session_chunks
               (session_id, commit_hash, chunk_type, chunk_text,
                payload_json, file_path, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("s1", "abc", "request", "test request", "{}", "", emb),
        )
        conn.commit()

        # Simulate a pre-v2 DB so the migration's backfill re-runs on reopen
        conn.execute("PRAGMA user_version = 0")
        conn.commit()

        # Re-open to trigger migration (vec backfill)
        store2 = SQLiteStorage(str(db))
        conn2 = store2._get_conn()
        count = conn2.execute("SELECT COUNT(*) FROM session_chunks_vec").fetchone()[0]
        assert count == 1


class TestKnowledgeCRUD:
    def test_store_and_query_knowledge_chunks(self, tmp_path):
        import struct

        from live_edit.storage import SQLiteStorage

        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))
        emb = struct.pack("384f", *[0.1] * 384)

        chunks = [
            {
                "source_path": "api:test",
                "chunk_index": 0,
                "chunk_text": "some knowledge",
                "embedding_bytes": emb,
                "metadata_json": '{"title": "Test"}',
            }
        ]
        store.store_knowledge_chunks("api:test", chunks)
        store.upsert_knowledge_meta("api:test", "api", None, 1)

        rows = store.query_knowledge_chunks(limit=10)
        assert len(rows) == 1
        assert rows[0][1] == "api:test"

    def test_delete_knowledge_chunks(self, tmp_path):
        import struct

        from live_edit.storage import SQLiteStorage

        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))
        emb = struct.pack("384f", *[0.1] * 384)

        store.store_knowledge_chunks(
            "api:test",
            [
                {
                    "source_path": "api:test",
                    "chunk_index": 0,
                    "chunk_text": "x",
                    "embedding_bytes": emb,
                    "metadata_json": "{}",
                }
            ],
        )
        store.upsert_knowledge_meta("api:test", "api", None, 1)
        store.delete_knowledge_chunks("api:test")
        store.delete_knowledge_meta("api:test")

        rows = store.query_knowledge_chunks(limit=10)
        assert len(rows) == 0

    def test_list_knowledge_meta(self, tmp_path):
        from live_edit.storage import SQLiteStorage

        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))

        store.upsert_knowledge_meta("doc1.md", "file", "abc123", 3)
        store.upsert_knowledge_meta("doc2.md", "file", "def456", 1)

        meta = store.list_knowledge_meta()
        assert len(meta) == 2
        paths = {m["source_path"] for m in meta}
        assert paths == {"doc1.md", "doc2.md"}

    def test_update_chunk_hit_counts(self, tmp_path):
        import struct

        from live_edit.storage import SQLiteStorage

        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))
        emb = struct.pack("384f", *[0.1] * 384)
        conn = store._get_conn()
        conn.execute(
            """INSERT INTO session_chunks
               (session_id, commit_hash, chunk_type, chunk_text,
                payload_json, file_path, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("s1", "abc", "request", "req", "{}", "", emb),
        )
        conn.commit()
        chunk_id = conn.execute("SELECT id FROM session_chunks").fetchone()[0]

        store.update_chunk_hit_counts([chunk_id])

        row = conn.execute(
            "SELECT hit_count, last_accessed FROM session_chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        assert row["hit_count"] == 1
        assert row["last_accessed"] is not None
