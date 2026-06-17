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
