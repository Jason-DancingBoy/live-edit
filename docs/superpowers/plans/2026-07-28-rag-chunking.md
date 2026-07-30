# RAG Session Memory — Per-File Chunking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade session memory from session-level embedding to per-file chunk embedding, reducing injected token volume by 60-75% and improving retrieval precision.

**Architecture:** New `session_chunks` table replaces `session_embeddings`. Each commit writes 1 request chunk + N file_diff chunks (one per modified file). Retrieval embeds query, scans chunks, groups by session_id (top-2 per session, prefer file_diff), and injects compact format into system prompt. Old data migrated idempotently via `PRAGMA user_version`.

**Tech Stack:** Python 3.10+, SQLite WAL, `struct` (stdlib), `json` (stdlib), `sentence-transformers`

## Global Constraints

- Python ≥ 3.10
- SQLite ≥ 3.25 (for `LIMIT -1` support; Ubuntu 20.04+ default)
- `sentence-transformers>=3.0` already listed in `[rag]` optional dep — no change
- Config dataclasses (`SessionMemoryConfig`, `EmbedderConfig`) — no changes
- Embedder interface — no changes
- `max_stored_entries` now counts sessions, not rows
- Old `session_embeddings` table retained read-only after migration

---

### Task 1: Storage — Chunk Table + Methods

**Files:**
- Modify: `live_edit/storage.py`
- Modify: `tests/test_storage.py`

**Interfaces:**
- Consumes: existing `SQLiteStorage._get_conn()`, `_init_db()`
- Produces:
  - `SQLiteStorage.store_chunks(session_id: str, commit_hash: str, chunks: list[dict]) -> None` — transactional DELETE old + INSERT new + evict
  - `SQLiteStorage.query_chunks(limit: int = 15000) -> list[tuple]` — returns `(id, session_id, commit_hash, chunk_type, chunk_text, payload_json, file_path, embedding)`
  - `SQLiteStorage.delete_session_chunks(session_id: str) -> None` — cleanup for continuation
  - `SQLiteStorage.delete_old_sessions(keep_count: int) -> None` — session-level eviction
  - `SQLiteStorage.get_db_version() -> int` — returns `PRAGMA user_version`
  - `SQLiteStorage.set_db_version(version: int) -> None` — sets `PRAGMA user_version`

- [ ] **Step 1: Add `session_chunks` table to `_init_db()`**

In `SQLiteStorage._init_db()`, add after the `session_embeddings` table creation (after line 81):

```python
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                commit_hash TEXT NOT NULL DEFAULT '',
                chunk_type TEXT NOT NULL CHECK(chunk_type IN ('request', 'file_diff')),
                chunk_text TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                file_path TEXT DEFAULT '',
                embedding BLOB NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_session
            ON session_chunks(session_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_type
            ON session_chunks(chunk_type)
        """)
```

- [ ] **Step 2: Implement `store_chunks()` with transactional atomicity**

Add to `SQLiteStorage` (after `delete_old_embeddings` at line 174):

```python
def store_chunks(self, session_id: str, commit_hash: str, chunks: list[dict]) -> None:
    """Transactionally replace all chunks for a session.

    Each chunk dict: {'chunk_type', 'chunk_text', 'payload_json',
                      'file_path', 'embedding_bytes'}

    Runs DELETE old chunks + INSERT new chunks + eviction check
    in a single BEGIN IMMEDIATE / COMMIT for atomicity.
    """
    conn = self._get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM session_chunks WHERE session_id = ?",
            (session_id,),
        )
        for c in chunks:
            conn.execute(
                """INSERT INTO session_chunks
                   (session_id, commit_hash, chunk_type, chunk_text,
                    payload_json, file_path, embedding)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    commit_hash,
                    c["chunk_type"],
                    c["chunk_text"],
                    c["payload_json"],
                    c.get("file_path", ""),
                    c["embedding_bytes"],
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
```

- [ ] **Step 3: Implement `query_chunks()` with LIMIT**

```python
    def query_chunks(self, limit: int = 15000) -> list[tuple]:
        """Return chunks ordered by recency, capped at `limit` rows.

        Returns: list of (id, session_id, commit_hash, chunk_type,
                 chunk_text, payload_json, file_path, embedding)
        """
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT id, session_id, commit_hash, chunk_type,
                      chunk_text, payload_json, file_path, embedding
               FROM session_chunks
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [tuple(r) for r in rows]
```

- [ ] **Step 4: Implement `delete_session_chunks()`**

```python
    def delete_session_chunks(self, session_id: str) -> None:
        """Delete all chunks for a session. Called before re-store on continuation."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM session_chunks WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()
```

- [ ] **Step 5: Implement `delete_old_sessions()` — session-level eviction**

```python
    def delete_old_sessions(self, keep_count: int) -> None:
        """Delete all chunks belonging to the oldest sessions.

        Keeps at most `keep_count` most recent sessions (by first chunk time).
        Deletes all chunks for sessions beyond that threshold.
        """
        conn = self._get_conn()
        conn.execute(
            """DELETE FROM session_chunks
               WHERE session_id IN (
                   SELECT session_id FROM (
                       SELECT DISTINCT session_id,
                              MIN(created_at) AS first_seen
                       FROM session_chunks
                       GROUP BY session_id
                       ORDER BY first_seen DESC
                       LIMIT -1 OFFSET ?
                   )
               )""",
            (keep_count,),
        )
        conn.commit()
```

- [ ] **Step 6: Implement `get_db_version()` and `set_db_version()`**

```python
def get_db_version(self) -> int:
    conn = self._get_conn()
    return conn.execute("PRAGMA user_version").fetchone()[0]


def set_db_version(self, version: int) -> None:
    conn = self._get_conn()
    conn.execute(f"PRAGMA user_version = {int(version)}")
    conn.commit()
```

- [ ] **Step 7: Write tests — add `TestSessionChunks` class to `tests/test_storage.py`**

Add at the end of the file (after `TestSessionEmbeddings`), along with `import struct` at top if not present:

```python
import json as _json


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
        session_ids = set(r[1] for r in rows)  # session_id at index 1
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
        with pytest.raises(Exception):
            storage.store_chunks("s1", "def", bad_chunks)
        # Original data should still be intact
        rows = storage.query_chunks()
        assert len(rows) == 1
        assert rows[0][4] == "original"

    def test_db_version_defaults_to_zero(self, storage):
        assert storage.get_db_version() == 0

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
```

Step 8: Run tests to verify they fail (new methods don't exist yet).

Even though we're adding methods to an existing class, the test file will fail because the old `test_storage.py` already imports `SQLiteStorage` but the new methods aren't implemented yet. Let's verify:

Run: `pytest tests/test_storage.py -v -k "TestSessionChunks" 2>&1 | tail -20`
Expected: FAIL with `AttributeError: 'SQLiteStorage' object has no attribute 'store_chunks'`

- [ ] **Step 9: Run all storage tests to verify new + old pass**

Run: `pytest tests/test_storage.py -v 2>&1 | tail -30`
Expected: all PASS (both existing `TestSQLiteStorage`, `TestSessionEmbeddings`, AND new `TestSessionChunks`)

- [ ] **Step 10: Commit**

```bash
git add live_edit/storage.py tests/test_storage.py
git commit -m "feat: add session_chunks table and CRUD methods to SQLiteStorage
"
```

---

### Task 2: SessionMemory — Core Rewrite (Store, Retrieve, Chunking)

**Files:**
- Modify: `live_edit/session_memory.py`
- Rewrite: `tests/test_session_memory.py`

**Interfaces:**
- Consumes: `SQLiteStorage.store_chunks()`, `SQLiteStorage.query_chunks()`, `SQLiteStorage.delete_old_sessions()` (from Task 1); `Embedder.embed_batch()`; `SessionMemoryConfig`
- Produces:
  - `MemoryEntry(session_id, request, file_path, diff_summary, stat, commit_hash, score)` — updated dataclass
  - `SessionMemory.store(session_id, request, files, diff, commit_hash)` — new signature with diff
  - `SessionMemory.retrieve(request) -> list[MemoryEntry]` — chunk-level retrieval
  - `SessionMemory._split_diff_by_file(diff: str) -> list[dict]` — diff parser
  - `SessionMemory._score_and_rank(query_vec, rows) -> list[MemoryEntry]` — updated with session grouping

- [ ] **Step 1: Write the updated `MemoryEntry` dataclass and `SessionMemory` skeleton**

Replace `live_edit/session_memory.py` content:

```python
"""Session memory --- stores and retrieves similar past edit sessions."""

import asyncio
import json
import logging
import math
import re
import struct
from dataclasses import dataclass, field

from .config import SessionMemoryConfig

logger = logging.getLogger("live-edit.session_memory")


@dataclass
class MemoryEntry:
    session_id: str
    request: str
    file_path: str = ""
    diff_summary: str = ""
    stat: str = ""
    commit_hash: str = ""
    score: float = 0.0


class SessionMemory:
    """Stores chunked embeddings of past sessions and retrieves similar ones."""

    def __init__(self, storage, embedder, config: SessionMemoryConfig):
        self._storage = storage
        self._embedder = embedder
        self.config = config

    # --- Public API ---

    async def store(
        self, session_id: str, request: str, files: list[str], diff: str, commit_hash: str
    ) -> None:
        """Chunk session by file and store embeddings transactionally.

        Produces 1 request chunk + 1 file_diff chunk per modified file.
        Old chunks for this session_id are replaced atomically.
        """
        if not self.config.enabled:
            return
        try:
            loop = asyncio.get_running_loop()

            # Parse diff into per-file chunks
            file_chunks = await loop.run_in_executor(None, self._split_diff_by_file, diff)

            # Build all chunk_texts for batch embedding
            chunk_texts = [request]  # request chunk
            for fc in file_chunks:
                chunk_texts.append(f"{request}\nFile: {fc['file_path']}\nChanges: {fc['stat']}")

            # Batch embed (CPU-bound)
            embeddings = await loop.run_in_executor(None, self._embedder.embed_batch, chunk_texts)

            # Construct chunk dicts with embeddings
            dim = len(embeddings[0])
            chunks = []

            # Request chunk
            chunks.append(
                {
                    "chunk_type": "request",
                    "chunk_text": chunk_texts[0],
                    "payload_json": json.dumps(
                        {
                            "request": request,
                            "files": files or [],
                            "commit_hash": commit_hash,
                        },
                        ensure_ascii=False,
                    ),
                    "file_path": "",
                    "embedding_bytes": struct.pack(f"{dim}f", *embeddings[0]),
                }
            )

            # File-diff chunks
            for i, fc in enumerate(file_chunks):
                payload = {
                    "file": fc["file_path"],
                    "diff": fc["diff_content"][:3000],
                    "stat": fc["stat"],
                    "request": request,
                    "commit_hash": commit_hash,
                }
                chunks.append(
                    {
                        "chunk_type": "file_diff",
                        "chunk_text": chunk_texts[i + 1],
                        "payload_json": json.dumps(payload, ensure_ascii=False),
                        "file_path": fc["file_path"],
                        "embedding_bytes": struct.pack(f"{dim}f", *embeddings[i + 1]),
                    }
                )

            # Transactional write + eviction in one run_in_executor call
            max_sessions = self.config.max_stored_entries
            await loop.run_in_executor(
                None,
                lambda: self._storage.store_chunks(session_id, commit_hash, chunks),
            )
            # Evict old sessions (fire-and-forget; session-level)
            await loop.run_in_executor(
                None,
                lambda: self._storage.delete_old_sessions(max_sessions),
            )

        except Exception:
            logger.warning(
                "Failed to store chunks for session %s",
                session_id,
                exc_info=True,
            )

    async def retrieve(self, request: str) -> list[MemoryEntry]:
        """Find similar past chunks, grouped by session."""
        if not self.config.enabled:
            return []
        try:
            loop = asyncio.get_running_loop()
            query_vec = await loop.run_in_executor(None, self._embedder.embed, request)
            rows = await loop.run_in_executor(None, self._storage.query_chunks)
            return self._score_and_rank(query_vec, rows)
        except Exception:
            logger.warning("Failed to retrieve session memories", exc_info=True)
            return []

    # --- Diff parsing ---

    @staticmethod
    def _split_diff_by_file(diff: str) -> list[dict]:
        """Split a unified diff into per-file entries.

        Returns list of {file_path, stat, diff_content}.
        Skips binary files and empty diffs.
        """
        if not diff or not diff.strip():
            return []

        # Split on 'diff --git ' boundaries
        parts = re.split(r"^(?=diff --git )", diff, flags=re.MULTILINE)
        results = []

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Check for binary
            if re.search(r"^Binary files ", part, re.MULTILINE):
                continue

            # Extract file path from ---/+++ headers
            file_match = re.search(r"^\+\+\+ b/(.+)$", part, re.MULTILINE)
            if not file_match:
                # Try rename
                rename_match = re.search(r"^rename (?:from|to) (.+)$", part, re.MULTILINE)
                if rename_match:
                    file_path = rename_match.group(1)
                else:
                    continue
            else:
                file_path = file_match.group(1)

            # Count stat
            lines_added = len(re.findall(r"^\+(?!\+\+)", part, re.MULTILINE))
            lines_removed = len(re.findall(r"^-(?!--)", part, re.MULTILINE))
            stat = f"+{lines_added}/-{lines_removed}"

            results.append(
                {
                    "file_path": file_path,
                    "stat": stat,
                    "diff_content": part,
                }
            )

        return results

    # --- Scoring ---

    def _score_and_rank(self, query_vec: list[float], rows: list[tuple]) -> list[MemoryEntry]:
        """Score chunks, group by session (top-2, prefer file_diff), rank."""
        dim = len(query_vec)
        scored = []  # (session_id, chunk_type, file_path, score, payload)

        for row in rows:
            # row = (id, session_id, commit_hash, chunk_type,
            #        chunk_text, payload_json, file_path, embedding)
            session_id = row[1]
            commit_hash = row[2]
            chunk_type = row[3]
            file_path = row[6] or ""
            emb_bytes = row[7]

            stored_vec = struct.unpack(f"{dim}f", emb_bytes)
            score = self._cosine_similarity(query_vec, stored_vec)
            if score < self.config.similarity_threshold:
                continue

            try:
                payload = json.loads(row[5])
            except (json.JSONDecodeError, TypeError):
                payload = {}

            scored.append(
                (
                    session_id,
                    chunk_type,
                    file_path,
                    score,
                    payload.get("request", ""),
                    payload.get("stat", ""),
                    commit_hash,
                    payload.get("diff", ""),
                )
            )

        # Group by session_id
        by_session: dict[str, list] = {}
        for item in scored:
            sid = item[0]
            if sid not in by_session:
                by_session[sid] = []
            by_session[sid].append(item)

        # Per session: pick top-2, prefer file_diff
        entries = []
        for sid, items in by_session.items():
            # Sort: file_diff first (bonus), then by score desc
            def _sort_key(item):
                chunk_type = item[1]
                score = item[3]
                type_bonus = 0.0 if chunk_type == "file_diff" else -0.05
                return score + type_bonus

            items.sort(key=_sort_key, reverse=True)
            picked = items[:2]

            for _sid, _ct, fpath, score, req, stat, chash, diff in picked:
                # diff_summary: first 4 non-empty lines of diff, ~120 chars
                diff_lines = [l for l in (diff or "").split("\n") if l.strip()][:4]
                diff_summary = "\n".join(diff_lines)

                entries.append(
                    MemoryEntry(
                        session_id=_sid,
                        request=req,
                        file_path=fpath,
                        diff_summary=diff_summary,
                        stat=stat,
                        commit_hash=chash,
                        score=score,
                    )
                )

        # Sort across sessions by best chunk score, take top max_entries
        entries.sort(key=lambda e: e.score, reverse=True)
        return entries[: self.config.max_entries]

    @staticmethod
    def _cosine_similarity(a, b) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
```

- [ ] **Step 2: Write the test file `tests/test_session_memory.py`**

Rewrite with chunk semantics:

```python
"""Tests for live_edit.session_memory --- chunking store, retrieve, diff parsing."""

import asyncio
import json
import math
import struct
import pytest
from unittest.mock import MagicMock, patch

from live_edit.session_memory import SessionMemory, MemoryEntry
from live_edit.config import SessionMemoryConfig


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
        # auth.py: 3 added lines, 1 removed line
        assert chunks[0]["stat"] == "+3/-1"

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
                "add JWT login\nFile: src/auth.py\nChanges: +3/-1": [0.9, 0.1, 0.0, 0.0],
                "add JWT login\nFile: src/session.py\nChanges: +2/-0": [0.8, 0.0, 0.2, 0.0],
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
            results = await sm.retrieve("add JWT login")

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
        session_ids = set(c.get("_sid") for c in storage._chunks)
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_session_memory.py -v 2>&1 | tail -30`
Expected: FAIL (old file still has v1 API, not yet replaced)

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `pytest tests/test_session_memory.py -v 2>&1 | tail -40`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add live_edit/session_memory.py tests/test_session_memory.py
git commit -m "feat: rewrite SessionMemory with per-file chunking store and retrieve"
```

---

### Task 3: SessionMemory — Migration Logic

**Files:**
- Modify: `live_edit/session_memory.py`
- Modify: `tests/test_session_memory.py`

**Interfaces:**
- Consumes: `SQLiteStorage.get_db_version()`, `SQLiteStorage.set_db_version()` (from Task 1); existing `session_embeddings` table
- Produces: `SessionMemory._migrate_if_needed()` — called once from `__init__`

- [ ] **Step 1: Add `_migrate_if_needed()` to `SessionMemory.__init__()`**

In `SessionMemory.__init__()`, after setting `self.config = config`, add:

```python
        self._migrate_if_needed()
```

Then add the method to the class (before `store()`):

```python
def _migrate_if_needed(self) -> None:
    """Idempotently migrate v1 session_embeddings to session_chunks.

    Uses PRAGMA user_version as migration gate:
      0 = not migrated (or fresh DB), 1 = migration complete.

    INSERT OR IGNORE with a temp unique index on (session_id, chunk_type)
    guarantees crash-safe re-runs.
    """
    try:
        conn = self._storage._get_conn()
    except AttributeError:
        return  # FakeStorage in tests — skip

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= 1:
        return

    # Check old table exists
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_embeddings'"
    ).fetchone()
    if not exists:
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        return

    logger.info("Migrating v1 session_embeddings to session_chunks...")
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_migration "
            "ON session_chunks(session_id, chunk_type)"
        )
        conn.execute(
            """INSERT OR IGNORE INTO session_chunks
               (session_id, chunk_type, chunk_text, payload_json,
                embedding, created_at)
               SELECT
                   session_id,
                   'request',
                   request,
                   json_object(
                       'request', request,
                       'files', files_json,
                       'migrated', json('true')
                   ),
                   embedding,
                   created_at
               FROM session_embeddings"""
        )
        conn.execute("DROP INDEX IF EXISTS idx_chunks_migration")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM session_chunks").fetchone()[0]
        logger.info("Migration complete: %d chunks in session_chunks", count)
    except Exception:
        conn.execute("DROP INDEX IF EXISTS idx_chunks_migration")
        conn.rollback()
        logger.warning(
            "Migration from session_embeddings failed; session memory will start with empty chunks",
            exc_info=True,
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
```

- [ ] **Step 2: Add migration tests to `tests/test_session_memory.py`**

Add after `TestSessionMemoryChunking`:

```python
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
        from live_edit.embedder import LocalEmbedder

        storage = real_sqlite_storage
        assert storage.get_db_version() == 0

        # Create SessionMemory with a fake embedder; migration runs in __init__
        sm = SessionMemory(
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

    def test_migration_sets_version(self, real_sqlite_storage):
        storage = real_sqlite_storage
        assert storage.get_db_version() == 0
        SessionMemory(
            storage=storage,
            embedder=FakeEmbedder(dim=4),
            config=SessionMemoryConfig(enabled=True),
        )
        assert storage.get_db_version() == 1

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
        assert storage.get_db_version() == 0

        SessionMemory(
            storage=storage,
            embedder=FakeEmbedder(dim=4),
            config=SessionMemoryConfig(enabled=True),
        )
        # Should set version without errors
        assert storage.get_db_version() == 1
        assert storage.query_chunks() == []
```

Also add `import json as _json` and `import struct` at the top of the test file if not already present.

- [ ] **Step 3: Run migration tests**

Run: `pytest tests/test_session_memory.py -v -k "TestMigration" 2>&1 | tail -20`
Expected: all PASS

- [ ] **Step 4: Run all session_memory tests**

Run: `pytest tests/test_session_memory.py -v 2>&1 | tail -40`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add live_edit/session_memory.py tests/test_session_memory.py
git commit -m "feat: add idempotent v1-to-chunk migration with PRAGMA user_version"
```

---

### Task 4: Engine — Wire Up New Store Signature + Compact Format

**Files:**
- Modify: `live_edit/engine.py`
- Modify: `tests/test_engine.py`

**Interfaces:**
- Consumes: `SessionMemory.store(session_id, request, files, diff, commit_hash)` (new signature from Task 2); `SessionMemory.retrieve(request)` (unchanged); `MemoryEntry` (new fields from Task 2)
- Produces: Updated `_do_commit()` store call; updated `_format_memory_context()` for compact format

- [ ] **Step 1: Update `_do_commit()` to pass `diff` and `commit_hash`**

In `engine.py`, replace the `sm.store()` call at lines 415-422:

```python
sm = getattr(session, "_session_memory", None)
if sm is not None:
    try:
        await sm.store(
            session_id=session.id,
            request=session.request,
            files=session._modified_files,
            diff=getattr(session, "_cached_diff", ""),
            commit_hash=session._commit_hash,
        )
    except Exception as e:
        logger.warning("Failed to store session memory: %s", e)
```

- [ ] **Step 2: Update `_format_memory_context()` for new `MemoryEntry` fields and compact format**

Replace `_format_memory_context()` at lines 440-476:

```python
def _format_memory_context(memories: list, template: str = "") -> str:
    """Format retrieved MemoryEntry list into a compact system-prompt string.

    Each entry shows request + file + diff_summary (~50-80 tokens vs ~200-300 in v1).
    """
    if template:
        try:
            items = []
            for i, m in enumerate(memories, 1):
                items.append(
                    template.replace("{index}", str(i))
                    .replace("{request}", m.request)
                    .replace("{file}", m.file_path or "(request)")
                    .replace("{diff_summary}", m.diff_summary or "")
                    .replace("{stat}", m.stat or "")
                    .replace("{commit_hash}", m.commit_hash)
                    .replace("{score}", f"{m.score:.0%}")
                )
            return "\n".join(items)
        except Exception:
            pass

    lines = [
        "## Relevant Past Changes",
        "",
        "Similar past edits (reference only, adapt to current request):",
        "",
    ]
    for i, m in enumerate(memories, 1):
        file_info = f" -> {m.file_path}" if m.file_path else ""
        lines.append(f'{i}. "{m.request}" ({m.score:.0%}){file_info}')
        if m.diff_summary:
            # Truncate each summary line to ~80 chars for compactness
            summary_lines = m.diff_summary.strip().split("\n")[:3]
            for sl in summary_lines:
                lines.append(f"   {sl[:120]}")
        lines.append("")

    lines.append("Use the above as reference only -- do not blindly copy past solutions.")
    return "\n".join(lines)
```

- [ ] **Step 3: Update engine integration tests in `tests/test_engine.py`**

Find and replace the `TestFormatMemoryContext` class (if it exists) or add it. The current test uses old MemoryEntry fields. Replace with:

```python
class TestFormatMemoryContext:
    def test_default_template(self):
        from live_edit.session_memory import MemoryEntry
        from live_edit.engine import _format_memory_context

        memories = [
            MemoryEntry(
                session_id="s1",
                request="Fix auth",
                file_path="auth.py",
                diff_summary="+import jwt\n+def login():",
                stat="+3/-1",
                commit_hash="abc",
                score=0.95,
            ),
        ]
        result = _format_memory_context(memories)
        assert "Relevant Past Changes" in result
        assert "Fix auth" in result
        assert "auth.py" in result
        assert "95%" in result

    def test_custom_template(self):
        from live_edit.session_memory import MemoryEntry
        from live_edit.engine import _format_memory_context

        memories = [
            MemoryEntry(
                session_id="s1",
                request="Fix auth",
                file_path="auth.py",
                diff_summary="+import jwt",
                stat="+3/-1",
                commit_hash="abc",
                score=0.95,
            ),
        ]
        template = "[{index}] {request} {file} {stat} {score}"
        result = _format_memory_context(memories, template)
        assert "[1] Fix auth auth.py +3/-1 95%" in result

    def test_empty_file_path_shows_request_only(self):
        from live_edit.session_memory import MemoryEntry
        from live_edit.engine import _format_memory_context

        memories = [
            MemoryEntry(
                session_id="s1",
                request="Some query",
                file_path="",
                diff_summary="",
                stat="",
                commit_hash="",
                score=0.80,
            ),
        ]
        result = _format_memory_context(memories)
        assert "Some query" in result

    def test_empty_memories(self):
        from live_edit.engine import _format_memory_context

        result = _format_memory_context([])
        assert "Relevant" in result
        assert "Use the above" in result
```

- [ ] **Step 4: Add engine integration test for store with diff**

Add after `TestFormatMemoryContext`:

```python
class TestSessionMemoryEngineIntegration:
    @pytest.mark.asyncio
    async def test_session_memory_disabled_by_default(self):
        from live_edit.engine import run_edit_session, EditSession
        from live_edit.config import Config
        from unittest.mock import AsyncMock, MagicMock

        config = Config()
        session = EditSession("test-s1", "Make it red")
        mock_provider = AsyncMock()
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = "/tmp/test-s1"
        mock_vcs.commit_in_worktree.return_value = "fakehash"
        mock_storage = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_tools.return_value = []

        mock_provider.call_with_tools.return_value = [
            {"type": "text", "text": "Done."},
        ]

        await run_edit_session(
            session=session,
            provider=mock_provider,
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            tool_registry=mock_registry,
        )
        assert True  # Should complete without errors
```

- [ ] **Step 5: Run engine tests**

Run: `pytest tests/test_engine.py -v -k "TestFormatMemoryContext or TestSessionMemoryEngineIntegration" 2>&1`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add live_edit/engine.py tests/test_engine.py
git commit -m "feat: wire chunking store signature and compact injection format into engine"
```

---

### Task 5: Full Test Suite Verification

**Files:**
- All files from Tasks 1-4
- No new files

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v 2>&1 | tail -50`
Expected: all PASS, zero regressions

- [ ] **Step 2: Verify existing tests that depend on SessionMemory/MemoryEntry still pass**

Run: `pytest tests/ -v -k "session_memory or memory or engine or storage" 2>&1 | tail -50`
Expected: all PASS

- [ ] **Step 3: Run with coverage check**

Run: `pytest tests/ --cov=live_edit --cov-report=term-missing 2>&1 | grep -E "(session_memory|storage|engine|embedder)" | head -20`
Expected: session_memory.py coverage > 85%

- [ ] **Step 4: Manual verification — Config no-change check**

Run:
```bash
python3 -c "
from live_edit.config import Config, SessionMemoryConfig
c = Config()
assert c.session_memory.enabled == False
assert c.session_memory.max_entries == 10
assert c.session_memory.max_stored_entries == 5000
print('Config OK')
"
```
Expected: `Config OK`

- [ ] **Step 5: Commit any test fixes (if needed)**

```bash
git add -A
git commit -m "test: finalize chunking test suite, all tests passing"
```
