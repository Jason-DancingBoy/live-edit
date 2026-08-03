"""Storage interface and default SQLite implementation for session persistence."""

import contextlib
import json
import logging
import re
import sqlite3
import threading
from abc import ABC, abstractmethod

logger = logging.getLogger("live-edit.storage")


class Storage(ABC):
    """Edit session persistence interface."""

    @abstractmethod
    def save_session(
        self,
        session_id: str,
        request: str,
        committed: bool,
        files: list[str],
        commit_hash: str,
        messages_json: str,
        mode: str,
    ) -> None: ...

    @abstractmethod
    def get_sessions(self, limit: int = 30) -> list[dict]: ...

    @abstractmethod
    def get_session_detail(self, session_id: str) -> dict | None: ...

    @abstractmethod
    def store_embedding(
        self, session_id: str, request: str, files_json: str, embedding: bytes
    ) -> None:
        """Store a session embedding for later retrieval."""

    @abstractmethod
    def query_embeddings(self) -> list[tuple[str, str, str, bytes]]:
        """Return all stored embeddings as (session_id, request, files_json, embedding_blob)."""

    @abstractmethod
    def delete_old_embeddings(self, keep_count: int) -> None:
        """Delete oldest embeddings, keeping at most `keep_count` most recent rows."""


class SQLiteStorage(Storage):
    """Default: SQLite-based session storage."""

    def __init__(self, db_path: str = "live_edit.db"):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            # Load sqlite-vec extension (optional; brute-force fallback if unavailable)
            try:
                import sqlite_vec  # type: ignore[import-not-found]

                self._local.conn.enable_load_extension(True)
                sqlite_vec.load(self._local.conn)
            except Exception:
                pass
        return self._local.conn  # type: ignore[no-any-return]

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_edit_sessions (
                session_id TEXT PRIMARY KEY,
                request TEXT NOT NULL,
                committed INTEGER DEFAULT 0,
                files TEXT DEFAULT '[]',
                commit_hash TEXT DEFAULT '',
                messages TEXT DEFAULT '[]',
                mode TEXT DEFAULT 'quick',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
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
        # Knowledge base tables (v2)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now')),
                hit_count INTEGER NOT NULL DEFAULT 0,
                last_accessed TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_knowledge_source
            ON knowledge_chunks(source_path)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_meta (
                source_path TEXT PRIMARY KEY,
                source_type TEXT NOT NULL CHECK(source_type IN ('file', 'api')),
                file_hash TEXT,
                chunk_count INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        self._migrate_to_memory_v2()

    def save_session(
        self,
        session_id: str,
        request: str,
        committed: bool,
        files: list[str],
        commit_hash: str,
        messages_json: str,
        mode: str,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO live_edit_sessions
               (session_id, request, committed, files, commit_hash, messages, mode, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                session_id,
                request,
                int(committed),
                json.dumps(files, ensure_ascii=False),
                commit_hash,
                messages_json,
                mode,
            ),
        )
        conn.commit()

    def _parse_json_fields(self, detail: dict) -> None:
        """Parse JSON string fields (messages, files) into Python objects.

        Handles both JSON arrays and legacy comma-separated strings.
        """
        for field in ("messages", "files"):
            raw = detail.get(field)
            if isinstance(raw, str) and raw:
                try:
                    detail[field] = json.loads(raw)
                except json.JSONDecodeError:
                    # Legacy format: comma-separated values (e.g. "file1,file2")
                    if field == "files":
                        detail[field] = [f for f in raw.split(",") if f]
            elif isinstance(raw, str) and not raw:
                detail[field] = []

    def get_sessions(self, limit: int = 30) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM live_edit_sessions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        sessions = []
        for row in rows:
            d = dict(row)
            self._parse_json_fields(d)
            sessions.append(d)
        return sessions

    def get_session_detail(self, session_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM live_edit_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        detail = dict(row)
        self._parse_json_fields(detail)
        return detail

    def store_embedding(
        self, session_id: str, request: str, files_json: str, embedding: bytes
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO session_embeddings
               (session_id, request, files_json, embedding, created_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (session_id, request, files_json, embedding),
        )
        conn.commit()

    def query_embeddings(self) -> list[tuple[str, str, str, bytes]]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT session_id, request, files_json, embedding
               FROM session_embeddings
               ORDER BY created_at DESC"""
        ).fetchall()
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    def delete_old_embeddings(self, keep_count: int) -> None:
        conn = self._get_conn()
        conn.execute(
            """DELETE FROM session_embeddings
               WHERE id NOT IN (
                   SELECT id FROM session_embeddings
                   ORDER BY created_at DESC LIMIT ?
               )""",
            (keep_count,),
        )
        conn.commit()

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
            # Sync vec table (best-effort; skip if sqlite-vec unavailable)
            with contextlib.suppress(Exception):
                conn.execute(
                    """
                    INSERT INTO session_chunks_vec (rowid, embedding)
                    SELECT id, embedding FROM session_chunks
                    WHERE session_id = ?
                """,
                    (session_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def query_chunks(self, limit: int = 15000) -> list[tuple]:
        """Return chunks ordered by recency, capped at `limit` rows.

        Returns: list of (id, session_id, commit_hash, chunk_type,
                 chunk_text, payload_json, file_path, embedding,
                 hit_count, last_accessed)
        """
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT id, session_id, commit_hash, chunk_type,
                      chunk_text, payload_json, file_path, embedding,
                      hit_count, last_accessed
               FROM session_chunks
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [tuple(r) for r in rows]

    def delete_session_chunks(self, session_id: str) -> None:
        """Delete all chunks for a session. Called before re-store on continuation."""
        conn = self._get_conn()
        with contextlib.suppress(Exception):
            conn.execute(
                "DELETE FROM session_chunks_vec WHERE rowid IN "
                "(SELECT id FROM session_chunks WHERE session_id = ?)",
                (session_id,),
            )
        conn.execute(
            "DELETE FROM session_chunks WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()

    def delete_old_sessions(self, keep_count: int) -> None:
        """Delete all chunks belonging to the oldest sessions.

        Keeps at most `keep_count` most recent sessions (by first chunk time).
        Deletes all chunks for sessions beyond that threshold.
        """
        conn = self._get_conn()
        with contextlib.suppress(Exception):
            conn.execute(
                "DELETE FROM session_chunks_vec WHERE rowid IN "
                "(SELECT id FROM session_chunks WHERE session_id IN "
                "(SELECT session_id FROM (SELECT DISTINCT session_id, "
                " MIN(created_at) AS first_seen FROM session_chunks "
                " GROUP BY session_id ORDER BY first_seen DESC LIMIT -1 OFFSET ?)))",
                (keep_count,),
            )
        conn.execute(
            """DELETE FROM session_chunks
               WHERE session_id IN (
                   SELECT session_id FROM (
                       SELECT DISTINCT session_id,
                              MIN(created_at) AS first_seen
                       FROM session_chunks
                       GROUP BY session_id
                       ORDER BY first_seen DESC
                       -- SQLite: LIMIT -1 means "no limit", so this returns all rows
                       -- starting from offset keep_count (i.e., the sessions to delete)
                       LIMIT -1 OFFSET ?
                   )
               )""",
            (keep_count,),
        )
        conn.commit()

    def get_db_version(self) -> int:
        conn = self._get_conn()
        return conn.execute("PRAGMA user_version").fetchone()[0]  # type: ignore[no-any-return]

    def set_db_version(self, version: int) -> None:
        conn = self._get_conn()
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()

    def _migrate_to_memory_v2(self, embedder_dim: int = 384) -> None:
        """Idempotently migrate session_chunks for hit tracking + create vec tables."""
        conn = self._get_conn()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= 2:
            return

        # Add hit_count and last_accessed columns
        for col, defn in [
            ("hit_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_accessed", "TEXT"),
        ]:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(f"ALTER TABLE session_chunks ADD COLUMN {col} {defn}")

        # Create vec virtual tables
        try:
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS session_chunks_vec
                USING vec0(rowid INTEGER PRIMARY KEY, embedding FLOAT[{embedder_dim}])
            """)
        except Exception:
            logger.warning("sqlite-vec not available; vector index disabled")

        # Backfill existing embeddings
        with contextlib.suppress(Exception):
            count = conn.execute("SELECT COUNT(*) FROM session_chunks_vec").fetchone()[0]
            if count == 0:
                conn.execute("""
                    INSERT INTO session_chunks_vec (rowid, embedding)
                    SELECT id, embedding FROM session_chunks
                """)

        # Create knowledge vec table
        with contextlib.suppress(Exception):
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_vec
                USING vec0(rowid INTEGER PRIMARY KEY, embedding FLOAT[{embedder_dim}])
            """)

        conn.execute("PRAGMA user_version = 2")
        conn.commit()

    def _ensure_vec_dimension(self, embedder_dim: int) -> None:
        """Ensure vec tables use the current embedder dimension.

        Reads each vec table's CREATE statement from sqlite_master and extracts the
        declared FLOAT[N] dimension with a regex. If a table is missing or its
        declared dimension != embedder_dim, DROP it and recreate it with
        FLOAT[embedder_dim], then re-index from the parent tables' embedding BLOBs.
        Any failure (e.g. sqlite-vec unavailable) is swallowed so callers fall back
        to brute-force retrieval.
        """
        conn = self._get_conn()
        for vec_table, parent_table in [
            ("session_chunks_vec", "session_chunks"),
            ("knowledge_chunks_vec", "knowledge_chunks"),
        ]:
            try:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (vec_table,),
                ).fetchone()
                declared = None
                if row and row[0]:
                    m = re.search(r"FLOAT\[(\d+)\]", row[0])
                    declared = int(m.group(1)) if m else None
                if declared == embedder_dim:
                    continue
                # Drop and rebuild with the current dimension, then re-index
                conn.execute(f"DROP TABLE IF EXISTS {vec_table}")
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS {vec_table}
                    USING vec0(rowid INTEGER PRIMARY KEY, embedding FLOAT[{embedder_dim}])
                """)
                conn.execute(f"""
                    INSERT INTO {vec_table} (rowid, embedding)
                    SELECT id, embedding FROM {parent_table}
                """)
                conn.commit()
            except Exception:
                logger.warning(
                    "Could not ensure %s dimension %s; using brute-force fallback",
                    vec_table,
                    embedder_dim,
                )

    def _vec_table_exists(self, name: str) -> bool:
        """Return True if the given virtual table exists in sqlite_master."""
        row = (
            self._get_conn()
            .execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            )
            .fetchone()
        )
        return row is not None

    def store_knowledge_chunks(self, source_path: str, chunks: list[dict]) -> None:
        """Transactionally replace all chunks for a source_path."""
        conn = self._get_conn()
        vec_exists = self._vec_table_exists("knowledge_chunks_vec")
        conn.execute("BEGIN IMMEDIATE")
        try:
            if vec_exists:
                # Delete vec rows first (parent-table id subquery) so no orphan
                # vec rows remain; any failure below rolls back the whole
                # transaction, so there is no partial state.
                conn.execute(
                    "DELETE FROM knowledge_chunks_vec WHERE rowid IN "
                    "(SELECT id FROM knowledge_chunks WHERE source_path = ?)",
                    (source_path,),
                )
            conn.execute(
                "DELETE FROM knowledge_chunks WHERE source_path = ?",
                (source_path,),
            )

            for c in chunks:
                conn.execute(
                    """INSERT INTO knowledge_chunks
                       (source_path, chunk_index, chunk_text, embedding, metadata_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        source_path,
                        c["chunk_index"],
                        c["chunk_text"],
                        c["embedding_bytes"],
                        c.get("metadata_json", "{}"),
                    ),
                )
            if vec_exists:
                conn.execute(
                    """
                    INSERT INTO knowledge_chunks_vec (rowid, embedding)
                    SELECT id, embedding FROM knowledge_chunks
                    WHERE source_path = ?
                """,
                    (source_path,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def delete_knowledge_chunks(self, source_path: str) -> None:
        conn = self._get_conn()
        if self._vec_table_exists("knowledge_chunks_vec"):
            conn.execute(
                "DELETE FROM knowledge_chunks_vec WHERE rowid IN "
                "(SELECT id FROM knowledge_chunks WHERE source_path = ?)",
                (source_path,),
            )
        conn.execute(
            "DELETE FROM knowledge_chunks WHERE source_path = ?",
            (source_path,),
        )
        conn.commit()

    def query_knowledge_chunks(self, limit: int = 15000) -> list[tuple]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT id, source_path, chunk_index, chunk_text,
                      metadata_json, embedding, hit_count, last_accessed
               FROM knowledge_chunks
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [tuple(r) for r in rows]

    def query_knowledge_chunks_vec(self, query_embedding_bytes: bytes, limit: int) -> list[tuple]:
        """sqlite-vec coarse search on knowledge chunks."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT kc.id, kc.source_path, kc.chunk_index, kc.chunk_text,
                      kc.metadata_json, kc.embedding, kc.hit_count, kc.last_accessed,
                      vec.distance
               FROM knowledge_chunks_vec vec
               JOIN knowledge_chunks kc ON kc.id = vec.rowid
               WHERE vec.embedding MATCH ?
               ORDER BY vec.distance
               LIMIT ?""",
            (query_embedding_bytes, limit),
        ).fetchall()
        return [tuple(r) for r in rows]

    def upsert_knowledge_meta(
        self,
        source_path: str,
        source_type: str,
        file_hash: str | None,
        chunk_count: int,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO knowledge_meta
               (source_path, source_type, file_hash, chunk_count, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (source_path, source_type, file_hash, chunk_count),
        )
        conn.commit()

    def delete_knowledge_meta(self, source_path: str) -> None:
        conn = self._get_conn()
        conn.execute("DELETE FROM knowledge_meta WHERE source_path = ?", (source_path,))
        conn.commit()

    def list_knowledge_meta(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM knowledge_meta ORDER BY source_path").fetchall()
        return [dict(r) for r in rows]

    def update_chunk_hit_counts(self, chunk_ids: list[int]) -> None:
        conn = self._get_conn()
        conn.executemany(
            """UPDATE session_chunks
               SET hit_count = hit_count + 1,
                   last_accessed = datetime('now')
               WHERE id = ?""",
            [(cid,) for cid in chunk_ids],
        )
        conn.commit()

    def query_chunks_vec(
        self, query_embedding_bytes: bytes, limit: int, dimension: int
    ) -> list[tuple] | None:
        """sqlite-vec coarse search. Returns None if vec unavailable."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT sc.id, sc.session_id, sc.commit_hash, sc.chunk_type,
                          sc.chunk_text, sc.payload_json, sc.file_path, sc.embedding,
                          sc.hit_count, sc.last_accessed, vec.distance
                   FROM session_chunks_vec vec
                   JOIN session_chunks sc ON sc.id = vec.rowid
                   WHERE vec.embedding MATCH ?
                   ORDER BY vec.distance
                   LIMIT ?""",
                (query_embedding_bytes, limit),
            ).fetchall()
            return [tuple(r) for r in rows]
        except Exception:
            return None
