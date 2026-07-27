"""Storage interface and default SQLite implementation for session persistence."""

import json
import os
import sqlite3
import threading
from abc import ABC, abstractmethod


class Storage(ABC):
    """Edit session persistence interface."""

    @abstractmethod
    def save_session(
        self, session_id: str, request: str, committed: bool,
        files: list[str], commit_hash: str, messages_json: str,
        mode: str,
    ) -> None:
        ...

    @abstractmethod
    def get_sessions(self, limit: int = 30) -> list[dict]:
        ...

    @abstractmethod
    def get_session_detail(self, session_id: str) -> dict | None:
        ...

    @abstractmethod
    def store_embedding(self, session_id: str, request: str,
                        files_json: str, embedding: bytes) -> None:
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
        return self._local.conn

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
        conn.commit()

    def save_session(
        self, session_id: str, request: str, committed: bool,
        files: list[str], commit_hash: str, messages_json: str,
        mode: str,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO live_edit_sessions
               (session_id, request, committed, files, commit_hash, messages, mode, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                session_id, request, int(committed),
                json.dumps(files, ensure_ascii=False),
                commit_hash, messages_json, mode,
            ),
        )
        conn.commit()

    def _parse_json_fields(self, detail: dict) -> dict:
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

    def store_embedding(self, session_id: str, request: str,
                        files_json: str, embedding: bytes) -> None:
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

    def store_chunks(self, session_id: str, commit_hash: str,
                     chunks: list[dict]) -> None:
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
                        session_id, commit_hash,
                        c["chunk_type"], c["chunk_text"],
                        c["payload_json"], c.get("file_path", ""),
                        c["embedding_bytes"],
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

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

    def delete_session_chunks(self, session_id: str) -> None:
        """Delete all chunks for a session. Called before re-store on continuation."""
        conn = self._get_conn()
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

    def get_db_version(self) -> int:
        conn = self._get_conn()
        return conn.execute("PRAGMA user_version").fetchone()[0]

    def set_db_version(self, version: int) -> None:
        conn = self._get_conn()
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()
