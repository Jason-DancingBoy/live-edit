"""Append-only audit log for live-edit governance events.

Best-effort by design: a failed audit write logs a warning and returns 0;
it never raises into the caller or interrupts the agent flow.
"""

import json
import logging
import sqlite3
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("live-edit.audit")


@dataclass
class AuditEvent:
    action: str
    ts: str = ""
    actor: str = "anonymous"
    target: str = ""
    session_id: str = ""
    result: str = "ok"
    detail: dict = field(default_factory=dict)
    id: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "session_id": self.session_id,
            "result": self.result,
            "detail": self.detail,
        }


class AuditLog:
    """Append-only audit store interface."""

    def record(
        self,
        action: str,
        *,
        actor: str = "anonymous",
        target: str = "",
        session_id: str = "",
        result: str = "ok",
        detail: dict | None = None,
    ) -> int: ...

    def query(
        self,
        *,
        action: str | None = None,
        actor: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
    ) -> list[AuditEvent]: ...


class SQLiteAuditLog(AuditLog):
    """Append-only audit log backed by SQLite (shares the app's live_edit.db)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT 'ok',
                detail_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action_ts ON audit_events(action, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(actor)")
        conn.commit()

    def record(
        self,
        action: str,
        *,
        actor: str = "anonymous",
        target: str = "",
        session_id: str = "",
        result: str = "ok",
        detail: dict | None = None,
    ) -> int:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        detail_json = json.dumps(detail or {}, ensure_ascii=False)
        try:
            conn = self._get_conn()
            cur = conn.execute(
                """INSERT INTO audit_events
                   (ts, actor, action, target, session_id, result, detail_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ts, actor, action, target, session_id, result, detail_json),
            )
            conn.commit()
            return int(cur.lastrowid)
        except Exception:
            logger.warning(
                "Audit record failed (action=%s, session=%s): %s",
                action,
                session_id,
                traceback.format_exc(),
            )
            return 0

    def query(
        self,
        *,
        action: str | None = None,
        actor: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
    ) -> list[AuditEvent]:
        clauses: list[str] = []
        params: list[str] = []
        if action:
            clauses.append("action = ?")
            params.append(action)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if after:
            clauses.append("ts >= ?")
            params.append(after)
        if before:
            clauses.append("ts <= ?")
            params.append(before)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._get_conn().execute(
            f"SELECT * FROM audit_events{where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        events = []
        for row in rows:
            d = dict(row)
            try:
                detail = json.loads(d.get("detail_json") or "{}")
            except json.JSONDecodeError:
                detail = {}
            events.append(
                AuditEvent(
                    id=d["id"],
                    ts=d["ts"],
                    actor=d["actor"],
                    action=d["action"],
                    target=d.get("target", ""),
                    session_id=d.get("session_id", ""),
                    result=d.get("result", "ok"),
                    detail=detail,
                )
            )
        return events
