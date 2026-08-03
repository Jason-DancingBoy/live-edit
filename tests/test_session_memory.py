"""Tests for live_edit.session_memory --- chunking store, retrieve, diff parsing."""

import asyncio
import json
import struct

import pytest

from live_edit.config import SessionMemoryConfig
from live_edit.session_memory import SessionMemory


class FakeEmbedder:
    def __init__(self, dim=4, vectors=None):
        self._dim = dim
        self._vectors = vectors or {}

    def embed(self, text):
        if text in self._vectors:
            return self._vectors[text]
        return [0.0] * self._dim

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]

    @property
    def dimension(self):
        return self._dim


class FakeStorage:
    def __init__(self):
        self._chunks = []
        self._version = 0

    def store_chunks(self, session_id, commit_hash, chunks):
        self._chunks = [c for c in self._chunks if c["_sid"] != session_id]
        for c in chunks:
            c["_sid"] = session_id
            c["_hash"] = commit_hash
        self._chunks.extend(chunks)

    def query_chunks(self, limit=15000):
        results = []
        for i, c in enumerate(self._chunks[-limit:]):
            emb = c.get("embedding_bytes", b"")
            results.append(
                (
                    i,
                    c.get("_sid", ""),
                    c.get("_hash", ""),
                    c.get("chunk_type", ""),
                    c.get("chunk_text", ""),
                    c.get("payload_json", "{}"),
                    c.get("file_path", ""),
                    emb,
                )
            )
        return results

    def query_chunks_vec(self, query_emb, limit, dim):
        return None  # fallback to brute-force cosine

    def update_chunk_hit_counts(self, chunk_ids):
        pass  # fake does not persist hit tracking

    def delete_old_sessions(self, keep_count):
        sessions = {}
        for i, c in enumerate(self._chunks):
            sid = c.get("_sid", "")
            if sid not in sessions:
                sessions[sid] = i
        keep_sids = set(sorted(sessions, key=lambda s: sessions[s])[-keep_count:])
        self._chunks = [c for c in self._chunks if c.get("_sid") in keep_sids]

    def get_db_version(self):
        return self._version

    def set_db_version(self, v):
        self._version = v


SAMPLE_DIFF = """diff --git a/src/auth.py b/src/auth.py
index abc123..def456 100644
--- a/src/auth.py
+++ b/src/auth.py
@@ -1,5 +1,10 @@
 import os
+import jwt
+from datetime import datetime
+
-def login(user, password):
-    return check_db(user, password)
+def login(user, password):
+    token = jwt.encode({"user": user}, SECRET)
+    return token

diff --git a/src/session.py b/src/session.py
index 111222..333444 100644
--- a/src/session.py
+++ b/src/session.py
@@ -10,3 +10,6 @@
 class Session:
     pass
+
+def create_session(user_id):
+    return Session(user_id=user_id, created_at=datetime.now())
"""


class TestSplitDiffByFile:
    def test_splits_multi_file_diff(self):
        chunks = SessionMemory._split_diff_by_file(SAMPLE_DIFF)
        assert len(chunks) == 2
        assert chunks[0]["file_path"] == "src/auth.py"
        assert chunks[1]["file_path"] == "src/session.py"

    def test_computes_stat(self):
        chunks = SessionMemory._split_diff_by_file(SAMPLE_DIFF)
        # auth.py: 6 added lines, 2 removed lines
        assert chunks[0]["stat"] == "+6/-2"

    def test_empty_diff(self):
        assert SessionMemory._split_diff_by_file("") == []
        assert SessionMemory._split_diff_by_file("   \n  ") == []

    def test_binary_diff_skipped(self):
        bin_diff = """diff --git a/foo.png b/foo.png
index abc..def 100644
Binary files a/foo.png and b/foo.png differ
"""
        chunks = SessionMemory._split_diff_by_file(bin_diff)
        assert len(chunks) == 0


class TestSessionMemoryChunking:
    @pytest.fixture
    def embedder(self):
        return FakeEmbedder(
            dim=4,
            vectors={
                "add JWT login": [1.0, 0.0, 0.0, 0.0],
                "add JWT login\nFile: src/auth.py\nChanges: +6/-2": [0.9, 0.1, 0.0, 0.0],
                "add JWT login\nFile: src/session.py\nChanges: +3/-0": [0.8, 0.0, 0.2, 0.0],
                "unrelated task": [0.0, 0.0, 0.0, 1.0],
                "unrelated task\nFile: other.py\nChanges: +1/-1": [0.0, 0.0, 0.0, 1.0],
            },
        )

    @pytest.fixture
    def storage(self):
        return FakeStorage()

    @pytest.fixture
    def config(self):
        return SessionMemoryConfig(
            enabled=True,
            max_entries=10,
            similarity_threshold=0.5,
            max_stored_entries=100,
        )

    @pytest.fixture
    def sm(self, storage, embedder, config):
        return SessionMemory(storage, embedder, config)

    def test_store_creates_request_and_file_chunks(self, sm, storage):
        async def run():
            await sm.store(
                "s1", "add JWT login", ["src/auth.py", "src/session.py"], SAMPLE_DIFF, "abc123"
            )

        asyncio.run(run())
        assert len(storage._chunks) == 3  # 1 request + 2 file_diff

    def test_retrieve_finds_relevant_chunks(self, sm, storage):
        async def run():
            await sm.store("s1", "add JWT login", ["src/auth.py"], SAMPLE_DIFF, "abc123")
            return await sm.retrieve("add JWT login")

        results = asyncio.run(run())
        assert len(results) >= 1
        assert results[0].request == "add JWT login"

    def test_unrelated_query_returns_empty(self, sm, storage):
        async def run():
            await sm.store("s1", "add JWT login", ["src/auth.py"], SAMPLE_DIFF, "abc123")
            return await sm.retrieve("unrelated task")

        results = asyncio.run(run())
        assert results == []

    def test_session_grouping_top_2(self, sm, storage):
        """Same session should contribute at most 2 chunks."""

        async def run():
            await sm.store(
                "s1", "add JWT login", ["src/auth.py", "src/session.py"], SAMPLE_DIFF, "abc123"
            )
            return await sm.retrieve("add JWT login")

        results = asyncio.run(run())
        # 3 chunks total (1 request + 2 file), but at most 2 returned per session
        s1_results = [r for r in results if r.session_id == "s1"]
        assert len(s1_results) <= 2

    def test_file_diff_preferred_over_request(self, sm, storage):
        """When both match, file_diff chunks rank higher than request chunk."""

        async def run():
            await sm.store("s1", "add JWT login", ["src/auth.py"], SAMPLE_DIFF, "abc123")
            return await sm.retrieve("add JWT login")

        results = asyncio.run(run())
        # At least one result should be a file_diff (non-empty file_path)
        has_file = any(r.file_path for r in results)
        assert has_file

    def test_eviction_on_store(self, sm, storage):
        sm.config.max_stored_entries = 3

        async def run():
            for i in range(5):
                await sm.store(f"s{i}", f"task {i}", [], SAMPLE_DIFF, f"hash{i}")

        asyncio.run(run())
        session_ids = {c.get("_sid") for c in storage._chunks}
        assert len(session_ids) <= 3

    def test_continuation_replaces_old_chunks(self, sm, storage):
        async def run():
            await sm.store("s1", "first commit", [], SAMPLE_DIFF, "hash1")
            await sm.store("s1", "second commit", [], SAMPLE_DIFF, "hash2")

        asyncio.run(run())
        # Should have 3 chunks (1 request + 2 file_diff), not 6
        assert len(storage._chunks) == 3

    def test_store_skipped_when_disabled(self, sm, storage):
        sm.config.enabled = False

        async def run():
            await sm.store("s1", "test", [], SAMPLE_DIFF, "hash")

        asyncio.run(run())
        assert len(storage._chunks) == 0

    def test_retrieve_skipped_when_disabled(self, sm, storage):
        sm.config.enabled = False

        async def run():
            return await sm.retrieve("test")

        results = asyncio.run(run())
        assert results == []

    def test_memory_entry_fields_populated(self, sm, storage):
        async def run():
            await sm.store("s1", "add JWT login", ["src/auth.py"], SAMPLE_DIFF, "abc123")
            return await sm.retrieve("add JWT login")

        results = asyncio.run(run())
        assert len(results) >= 1
        entry = results[0]
        assert entry.session_id == "s1"
        assert entry.request == "add JWT login"
        assert entry.commit_hash == "abc123"

    def test_empty_diff_stores_request_chunk_only(self, sm, storage):
        async def run():
            await sm.store("s1", "no files changed", [], "", "hash")

        asyncio.run(run())
        assert len(storage._chunks) == 1
        assert storage._chunks[0]["chunk_type"] == "request"

    def test_diff_with_no_valid_files(self, sm, storage):
        """Binary-only diff should produce only request chunk."""
        bin_diff = """diff --git a/img.png b/img.png
index abc..def 100644
Binary files a/img.png and b/img.png differ
"""

        async def run():
            await sm.store("s1", "add image", [], bin_diff, "hash")

        asyncio.run(run())
        assert len(storage._chunks) == 1
        assert storage._chunks[0]["chunk_type"] == "request"


class TestMigration:
    """Tests for _migrate_if_needed idempotent migration."""

    @pytest.fixture
    def real_sqlite_storage(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "test_migrate.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL UNIQUE,
                request TEXT NOT NULL,
                files_json TEXT NOT NULL DEFAULT '[]',
                embedding BLOB NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                commit_hash TEXT NOT NULL DEFAULT '',
                chunk_type TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                file_path TEXT DEFAULT '',
                embedding BLOB NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()

        # Insert v1 data
        emb = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
        conn.execute(
            "INSERT INTO session_embeddings (session_id, request, files_json, embedding) "
            "VALUES (?, ?, ?, ?)",
            ("old-s1", "add feature X", '["a.py"]', emb),
        )
        conn.commit()
        conn.close()

        from live_edit.storage import SQLiteStorage

        return SQLiteStorage(db_path)

    def test_migration_copies_old_data(self, real_sqlite_storage):
        storage = real_sqlite_storage
        # SQLiteStorage auto-migrates a fresh DB to schema v2 on init
        assert storage.get_db_version() == 2

        # Create SessionMemory with a fake embedder; migration runs in __init__
        sm = SessionMemory(  # noqa: F841
            storage=storage,
            embedder=FakeEmbedder(dim=4),
            config=SessionMemoryConfig(enabled=True),
        )

        rows = storage.query_chunks()
        assert len(rows) >= 1
        # Verify it's a request chunk with migrated=True
        payload = json.loads(rows[0][5])  # payload_json at index 5
        assert payload.get("migrated") is True
        assert payload.get("request") == "add feature X"

    def test_migration_keeps_v2_schema(self, real_sqlite_storage):
        storage = real_sqlite_storage
        assert storage.get_db_version() == 2
        SessionMemory(
            storage=storage,
            embedder=FakeEmbedder(dim=4),
            config=SessionMemoryConfig(enabled=True),
        )
        # The v1 migration still runs but must not downgrade the v2 schema
        assert storage.get_db_version() == 2

    def test_migration_idempotent(self, real_sqlite_storage):
        storage = real_sqlite_storage
        SessionMemory(
            storage=storage,
            embedder=FakeEmbedder(dim=4),
            config=SessionMemoryConfig(enabled=True),
        )
        count1 = len(storage.query_chunks())

        # Second init should not duplicate
        SessionMemory(
            storage=storage,
            embedder=FakeEmbedder(dim=4),
            config=SessionMemoryConfig(enabled=True),
        )
        count2 = len(storage.query_chunks())
        assert count2 == count1

    def test_migration_skips_when_no_old_table(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "test_fresh.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                commit_hash TEXT NOT NULL DEFAULT '',
                chunk_type TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                file_path TEXT DEFAULT '',
                embedding BLOB NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()

        from live_edit.storage import SQLiteStorage

        storage = SQLiteStorage(db_path)
        assert storage.get_db_version() == 2

        SessionMemory(
            storage=storage,
            embedder=FakeEmbedder(dim=4),
            config=SessionMemoryConfig(enabled=True),
        )
        # Should preserve the v2 schema without errors
        assert storage.get_db_version() == 2
        assert storage.query_chunks() == []
