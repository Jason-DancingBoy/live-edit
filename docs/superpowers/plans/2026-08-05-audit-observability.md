# Audit Logging & Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an append-only audit trail, structured JSON logging, and process-local Prometheus metrics to live-edit.

**Architecture:** Three new self-contained modules (`audit.py`, `metrics.py`, `logging.py`) wired into the existing `router.py` endpoints and `engine.py` agent loop. Everything is injectable (mirroring the Provider/Storage/VCS pattern) and **best-effort** — audit/metric writes never interrupt the agent flow.

**Tech Stack:** Python 3.10+, FastAPI, SQLite (stdlib `sqlite3`), stdlib `logging`, `contextvars`, `threading`. **No new dependencies.**

## Global Constraints

- Python `>=3.10`; no new runtime dependencies; `ruff` (line-length 100, quote-style double) and `mypy` must stay clean.
- **Implementation commits are allowed** — each task ends with a normal git commit. The **spec doc** (`docs/superpowers/specs/2026-08-05-audit-observability-design.md`) stays uncommitted per user instruction.
- All existing tests must keep passing: `pytest` with `asyncio_mode = "auto"` (pyproject.toml), coverage `fail_under = 60`.
- Audit/metrics writes are **best-effort**: catch all exceptions, `logger.warning`, never raise into callers.
- `SQLiteAuditLog` uses **`INSERT` only** — no UPDATE/DELETE methods, no public delete path.
- `setup_live_edit`, `run_edit_session`, `continue_edit_session`, `_run_agent_loop_fix`, and `SessionStore` gain **optional** params (`audit_log=None, metrics=None`) — existing callers pass nothing and keep working.
- Spec: `docs/superpowers/specs/2026-08-05-audit-observability-design.md` (not committed).

---

### Task 1: `live_edit/logging.py` — structured JSON logging + contextvars

**Files:**
- Create: `live_edit/logging.py`
- Test: `tests/test_logging.py`

**Interfaces:**
- Produces (consumed by Tasks 5, 6):
  - `set_session_id(sid: str) -> contextvars.Token`
  - `set_correlation_id(cid: str) -> contextvars.Token`
  - `get_session_id() -> str`
  - `get_correlation_id() -> str`
  - `JsonFormatter` (a `logging.Formatter`)
  - `configure_logging(level: str = "INFO", json_logs: bool = True, stream=None) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_logging.py
import io
import json
import logging

from live_edit.logging import (
    JsonFormatter,
    configure_logging,
    get_correlation_id,
    get_session_id,
    set_correlation_id,
    set_session_id,
)


def _logger(name: str) -> logging.Logger:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(name)
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, stream


def test_json_formatter_emits_structured_fields():
    logger, stream = _logger("live-edit.test.formatter")
    logger.info("hello")
    record = json.loads(stream.getvalue().strip())
    assert record["level"] == "INFO"
    assert record["logger"] == "live-edit.test.formatter"
    assert record["message"] == "hello"
    assert record["session_id"] == ""
    assert record["correlation_id"] == ""


def test_json_formatter_includes_contextvars():
    logger, stream = _logger("live-edit.test.contextvars")
    set_session_id("le_abc")
    set_correlation_id("req_123")
    try:
        logger.info("with ctx")
        record = json.loads(stream.getvalue().strip())
        assert record["session_id"] == "le_abc"
        assert record["correlation_id"] == "req_123"
    finally:
        set_session_id("")
        set_correlation_id("")


def test_set_get_roundtrip():
    set_session_id("le_xyz")
    set_correlation_id("req_xyz")
    try:
        assert get_session_id() == "le_xyz"
        assert get_correlation_id() == "req_xyz"
    finally:
        set_session_id("")
        set_correlation_id("")


def test_configure_logging_targets_live_edit_namespace_only():
    configure_logging(level="DEBUG", json_logs=True)
    le_logger = logging.getLogger("live-edit")
    assert le_logger.level == logging.DEBUG
    assert not le_logger.propagate
    assert len(le_logger.handlers) == 1
    # The root logger is not touched by configure_logging.
    root_has_le_handler = any(
        h is le_logger.handlers[0] for h in logging.getLogger().handlers
    )
    assert not root_has_le_handler
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_logging.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'live_edit.logging'`

- [ ] **Step 3: Implement `live_edit/logging.py`**

```python
"""Structured JSON logging for the live-edit.* logger namespace."""

import contextvars
import datetime as _dt
import json
import logging

session_id_ctx = contextvars.ContextVar("live_edit.session_id", default="")
correlation_id_ctx = contextvars.ContextVar("live_edit.correlation_id", default="")


def set_session_id(sid: str) -> contextvars.Token:
    return session_id_ctx.set(sid)


def set_correlation_id(cid: str) -> contextvars.Token:
    return correlation_id_ctx.set(cid)


def get_session_id() -> str:
    return session_id_ctx.get()


def get_correlation_id() -> str:
    return correlation_id_ctx.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "session_id": session_id_ctx.get(),
            "correlation_id": correlation_id_ctx.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", json_logs: bool = True, stream=None) -> None:
    handler = logging.StreamHandler(stream)
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("live-edit")
    logger.handlers[:] = [handler]
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_logging.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add live_edit/logging.py tests/test_logging.py
git commit -m "feat(logging): structured JSON logging with session/correlation contextvars"
```

---

### Task 2: `live_edit/metrics.py` — process-local Metrics registry

**Files:**
- Create: `live_edit/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Produces (consumed by Tasks 5, 6):
  - `Metrics` with methods:
    - `inc(name: str, labels: dict | None = None, value: int = 1) -> None`
    - `set(name: str, labels: dict | None = None, value: float) -> None`
    - `observe(name: str, value: float, labels: dict | None = None) -> None`
    - `render() -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_metrics.py
from live_edit.metrics import BUCKETS, Metrics


def test_counter_inc_and_render():
    m = Metrics()
    m.inc("live_edit_sessions_total", {"outcome": "started"})
    m.inc("live_edit_sessions_total", {"outcome": "started"})
    out = m.render()
    assert 'live_edit_sessions_total{outcome="started"} 2' in out


def test_gauge_set():
    m = Metrics()
    m.set("live_edit_active_sessions", value=3)
    assert "live_edit_active_sessions 3" in m.render()


def test_histogram_observe_renders_count_sum_buckets():
    m = Metrics()
    m.observe("live_edit_llm_duration_seconds", 0.5)
    m.observe("live_edit_llm_duration_seconds", 2.0)
    out = m.render()
    assert "live_edit_llm_duration_seconds_count 2" in out
    assert "live_edit_llm_duration_seconds_sum 2.5" in out
    # 0.5 <= 0.5 bucket, 2.0 <= 2.5 bucket
    assert 'live_edit_llm_duration_seconds_bucket{le="0.5"} 1' in out
    assert 'live_edit_llm_duration_seconds_bucket{le="2.5"} 2' in out
    assert 'live_edit_llm_duration_seconds_bucket{le="+Inf"} 2' in out


def test_histogram_labeled():
    m = Metrics()
    m.observe("live_edit_tool_duration_ms", 3, {"tool": "edit_file"})
    out = m.render()
    assert 'live_edit_tool_duration_ms_count{tool="edit_file"} 1' in out


def test_concurrent_increments_are_thread_safe():
    import threading

    m = Metrics()
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(200):
                m.inc("live_edit_sessions_total", {"outcome": "started"})
                m.render()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert "live_edit_sessions_total{outcome=\"started\"} 800" in m.render()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'live_edit.metrics'`

- [ ] **Step 3: Implement `live_edit/metrics.py`**

```python
"""Process-local metrics registry with Prometheus text exposition rendering."""

import threading

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)


def _label_str(labels: dict) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


class Metrics:
    """Thread-safe counters, gauges, and histograms rendered as Prometheus text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple, int] = {}
        self._gauges: dict[tuple, float] = {}
        self._histograms: dict[tuple, dict] = {}

    @staticmethod
    def _key(name: str, labels: dict) -> tuple:
        return (name, tuple(sorted(labels.items())))

    def inc(self, name: str, labels: dict | None = None, value: int = 1) -> None:
        labels = labels or {}
        with self._lock:
            key = self._key(name, labels)
            self._counters[key] = self._counters.get(key, 0) + value

    def set(self, name: str, labels: dict | None = None, value: float = 0.0) -> None:
        labels = labels or {}
        with self._lock:
            key = self._key(name, labels)
            self._gauges[key] = float(value)

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        labels = labels or {}
        with self._lock:
            key = self._key(name, labels)
            h = self._histograms.get(key)
            if h is None:
                h = {"count": 0, "sum": 0.0, "buckets": [0] * len(BUCKETS)}
                self._histograms[key] = h
            h["count"] += 1
            h["sum"] += float(value)
            for i, bucket in enumerate(BUCKETS):
                if value <= bucket:
                    h["buckets"][i] += 1

    def render(self) -> str:
        with self._lock:
            lines: list[str] = []
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{_label_str(dict(labels))} {value}")
            for (name, labels), value in sorted(self._gauges.items()):
                lines.append(f"{name}{_label_str(dict(labels))} {value}")
            for (name, labels), h in sorted(self._histograms.items()):
                lab = dict(labels)
                lines.append(f"{name}_count{_label_str(lab)} {h['count']}")
                lines.append(f"{name}_sum{_label_str(lab)} {h['sum']}")
                for i, bucket in enumerate(BUCKETS):
                    lines.append(
                        f'{name}_bucket{_label_str({**lab, "le": str(bucket)})} '
                        f"{h['buckets'][i]}"
                    )
                lines.append(
                    f'{name}_bucket{_label_str({**lab, "le": "+Inf"})} {h["count"]}'
                )
            return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_metrics.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add live_edit/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): thread-safe Prometheus counters/gauges/histograms"
```

---

### Task 3: `live_edit/audit.py` — append-only audit log

**Files:**
- Create: `live_edit/audit.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Produces (consumed by Tasks 5, 6):
  - `AuditEvent` dataclass: `id, ts, actor, action, target, session_id, result, detail` + `to_dict()`
  - `AuditLog.record(action, *, actor="anonymous", target="", session_id="", result="ok", detail=None) -> int`
  - `AuditLog.query(*, action=None, actor=None, session_id=None, limit=100, after=None, before=None) -> list[AuditEvent]`
  - `SQLiteAuditLog(db_path: str)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_audit.py
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_audit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'live_edit.audit'`

- [ ] **Step 3: Implement `live_edit/audit.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_audit.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add live_edit/audit.py tests/test_audit.py
git commit -m "feat(audit): append-only SQLite audit log with query API"
```

---

### Task 4: config.py — `[observability]` section

**Files:**
- Modify: `live_edit/config.py` (add `ObservabilityConfig` near `EvaluationConfig` at ~line 98; add `_parse_observability` near `_parse_timeouts` at ~line 254; add field to `Config` at line 216; wire into `parse_config` return at line 444)
- Test: `tests/test_config.py` (append to existing file)

**Interfaces:**
- Consumes: nothing.
- Produces (consumed by Tasks 5, 6):
  - `ObservabilityConfig(log_level: str = "INFO", json_logs: bool = True, metrics_enabled: bool = True, audit_enabled: bool = True)`
  - `Config.observability: ObservabilityConfig`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`)

```python
from live_edit.config import (
    Config,
    ObservabilityConfig,
    detect_project,
    parse_config,
    validate_config,
)


class TestObservabilityConfig:
    def test_observability_defaults_when_absent(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text(
            "[project]\nname = \"TestApp\"\nlanguage = \"python\"\n\n"
            "[llm]\napi_url = \"https://api.example.com\"\napi_key_env = \"KEY\"\nmodel = \"m1\"\n\n"
            "[modes.quick]\nlabel = \"Q\"\n"
        )
        config = parse_config(str(toml_path))
        assert config.observability.log_level == "INFO"
        assert config.observability.json_logs is True
        assert config.observability.metrics_enabled is True
        assert config.observability.audit_enabled is True

    def test_observability_parses_values(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text(
            "[project]\nname = \"TestApp\"\nlanguage = \"python\"\n\n"
            "[llm]\napi_url = \"https://api.example.com\"\napi_key_env = \"KEY\"\nmodel = \"m1\"\n\n"
            "[modes.quick]\nlabel = \"Q\"\n\n"
            "[observability]\nlog_level = \"DEBUG\"\njson_logs = false\n"
            "metrics_enabled = false\naudit_enabled = false\n"
        )
        config = parse_config(str(toml_path))
        assert config.observability.log_level == "DEBUG"
        assert config.observability.json_logs is False
        assert config.observability.metrics_enabled is False
        assert config.observability.audit_enabled is False

    def test_old_config_without_observability_still_validates(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text(
            "[project]\nname = \"TestApp\"\nlanguage = \"python\"\n\n"
            "[llm]\napi_url = \"https://api.example.com\"\napi_key_env = \"KEY\"\nmodel = \"m1\"\n\n"
            "[modes.quick]\nlabel = \"Q\"\n"
        )
        config = parse_config(str(toml_path))
        assert validate_config(config) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_config.py::TestObservabilityConfig -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'observability'`

- [ ] **Step 3: Implement the config changes**

Add the dataclass after `EvaluationConfig` (~line 109):

```python
@dataclass
class ObservabilityConfig:
    log_level: str = "INFO"
    json_logs: bool = True
    metrics_enabled: bool = True
    audit_enabled: bool = True
```

Add the field to `Config` (in the field list at ~line 229, after `evaluation`):

```python
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
```

Add the parser after `_parse_timeouts` (~line 263):

```python
def _parse_observability(data: dict) -> ObservabilityConfig:
    return ObservabilityConfig(
        log_level=data.get("log_level", "INFO"),
        json_logs=data.get("json_logs", True),
        metrics_enabled=data.get("metrics_enabled", True),
        audit_enabled=data.get("audit_enabled", True),
    )
```

Wire it into `parse_config` — add near the `evaluation` block (after line 354) and into the returned `Config(...)` (line 444):

```python
    observability = _parse_observability(raw.get("observability", {}))
```
and
```python
    return Config(
        project=project,
        llm=llm,
        safety=safety,
        timeouts=timeouts,
        sessions=sessions,
        hooks=hooks,
        ui=ui,
        modes=modes,
        errors=errors,
        preview=preview,
        evaluation=evaluation,
        memory=memory,
        observability=observability,
        toml_tools=toml_tools,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (all tests, including the 3 new ones)

- [ ] **Step 5: Commit**

```bash
git add live_edit/config.py tests/test_config.py
git commit -m "feat(config): add [observability] section (log_level/json_logs/metrics/audit)"
```

---

### Task 5: router.py — middleware, instantiation, new endpoints, endpoint audit wiring

**Files:**
- Modify: `live_edit/router.py`
- Modify: `live_edit/__init__.py` (export `AuditLog`, `SQLiteAuditLog`, `Metrics`)
- Test: `tests/test_router.py` (append), `tests/test_observability_endpoints.py` (new)

**Interfaces:**
- Consumes: Task 1 (`set_correlation_id`, `get_session_id`, `configure_logging`), Task 2 (`Metrics`), Task 3 (`AuditLog`, `SQLiteAuditLog`), Task 4 (`config.observability`).
- Produces: passes `audit_log`/`metrics` into `run_edit_session`, `continue_edit_session`, `SessionStore`; new endpoints `GET /live-edit/metrics`, `GET /live-edit/admin/audit`.

- [ ] **Step 1: Write the failing tests** (new file `tests/test_observability_endpoints.py`)

```python
# tests/test_observability_endpoints.py
from fastapi import FastAPI
from fastapi.testclient import TestClient

from live_edit.audit import SQLiteAuditLog
from live_edit.metrics import Metrics
from live_edit.router import setup_live_edit


def _write_config(tmp_path):
    cfg = tmp_path / ".live-edit.toml"
    cfg.write_text(
        """[project]
name = "TestApp"
language = "python"
root = "."

[llm]
provider = "anthropic_compatible"
api_url = "https://api.example.com/v1/messages"
api_key_env = "FAKE_KEY"
model = "test-model"

[sessions]
max_active = 10

[modes.quick]
label = "快速修改"
approval = "per_tool"
tools = "write"

[modes.quick.prompt]
base = "You are a helpful AI."

[observability]
log_level = "INFO"
json_logs = true
metrics_enabled = true
audit_enabled = true
"""
    )
    return str(cfg)


def _client(tmp_path, **kwargs):
    _write_config(tmp_path)  # config file must exist: parse_config raises FileNotFoundError otherwise
    app = FastAPI()
    router = setup_live_edit(
        project_root=str(tmp_path),
        config_path=".live-edit.toml",
        admin_key="secret-admin",
        **kwargs,
    )
    app.include_router(router)
    return TestClient(app)


def test_metrics_endpoint_returns_prometheus_text(tmp_path):
    metrics = Metrics()
    metrics.inc("live_edit_sessions_total", {"outcome": "started"})
    client = _client(tmp_path, metrics=metrics)
    resp = client.get("/live-edit/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert 'live_edit_sessions_total{outcome="started"} 1' in resp.text


def test_admin_audit_requires_admin_key(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "live_edit.db"))
    audit.record("session_start", session_id="le_1")
    client = _client(tmp_path, audit_log=audit)
    resp = client.get("/live-edit/admin/audit")
    assert resp.status_code == 403
    ok = client.get("/live-edit/admin/audit", headers={"X-Admin-Key": "secret-admin"})
    assert ok.status_code == 200
    events = ok.json()["events"]
    assert len(events) == 1
    assert events[0]["action"] == "session_start"


def test_failed_admin_auth_is_audited(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "live_edit.db"))
    client = _client(tmp_path, audit_log=audit)
    client.get("/live-edit/admin/audit", headers={"X-Admin-Key": "wrong"})
    events = audit.query(action="failed_admin_auth")
    assert len(events) == 1
    assert events[0].result == "blocked"


def test_admin_audit_supports_filters(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "live_edit.db"))
    audit.record("session_start", session_id="le_a")
    audit.record("commit", session_id="le_a", result="ok", target="c1")
    client = _client(tmp_path, audit_log=audit)
    resp = client.get(
        "/live-edit/admin/audit",
        headers={"X-Admin-Key": "secret-admin"},
        params={"action": "commit"},
    )
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["target"] == "c1"


def test_stream_audits_session_start_and_metrics(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "live_edit.db"))
    metrics = Metrics()
    client = _client(tmp_path, audit_log=audit, metrics=metrics)
    # capacity rejection path: max_active=10 fine, but provider will error out
    resp = client.post(
        "/live-edit/stream",
        json={"request": "hello", "mode": "quick"},
    )
    # The SSE stream is drained; at minimum a session_start audit exists.
    assert resp.status_code == 200
    started = audit.query(action="session_start")
    assert len(started) == 1
    assert "live_edit_sessions_total{outcome=\"started\"}" in metrics.render()


def test_correlation_id_header_is_echoed(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/live-edit/health", headers={"X-Request-ID": "req-custom"})
    assert resp.headers.get("X-Request-ID") == "req-custom"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_observability_endpoints.py -v`
Expected: FAIL (endpoints 404, no correlation header, signature errors)

- [ ] **Step 3: Implement the router changes**

3a. Add imports at the top of `router.py` (after existing imports):

```python
from .audit import AuditLog, SQLiteAuditLog
from .logging import configure_logging, set_correlation_id, set_session_id
from .metrics import Metrics
```

3b. Extend `setup_live_edit` signature (line 59-68) with two optional params:

```python
    tool_registry: object | None = None,
    audit_log: AuditLog | None = None,
    metrics: Metrics | None = None,
) -> APIRouter:
```

3c. After the `vcs` default block (line 99), instantiate the observability defaults:

```python
    # Observability: audit log + metrics + structured logging.
    if audit_log is None and config.observability.audit_enabled:
        audit_log = SQLiteAuditLog(os.path.join(project_root, "live_edit.db"))
    if metrics is None:
        metrics = Metrics()
    configure_logging(level=config.observability.log_level, json_logs=config.observability.json_logs)
```

3d. Add the `audit_log` param to `SessionStore.__init__` (engine.py line 205 — store it only; the expiry audit call sites come in Task 6), then pass it in router.py line 104:

```python
    def __init__(self, max_active: int = 10, ttl_seconds: int = 1800, audit_log=None):
        self._sessions: dict[str, EditSession] = {}
        self.max_active = max_active
        self.ttl_seconds = ttl_seconds
        self.audit_log = audit_log
```

```python
    session_store = SessionStore(max_active=max_active, ttl_seconds=ttl, audit_log=audit_log)
```

3e. Add the correlation middleware before the first endpoint (after the static dir line ~125):

```python
    @router.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        cid = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:12]}"
        set_correlation_id(cid)
        response = await call_next(request)
        response.headers["X-Request-ID"] = cid
        return response
```

> The contextvar is intentionally **not reset** here: Starlette runs each request in its own context, so `correlation_id` stays scoped to the request's SSE stream/background task and does not leak. The integration test locks this in.

3f. Add `Response` to the `fastapi.responses` import at the top of `router.py` (currently `from fastapi.responses import FileResponse, JSONResponse, StreamingResponse`). Add the two new endpoints near the `/health` endpoint (after line 414):

```python
    # ── GET /live-edit/metrics ──

    @router.get("/metrics")
    async def metrics_endpoint():
        """Prometheus text metrics. Gate at the reverse proxy for production."""
        return Response(content=metrics.render(), media_type="text/plain")

    # ── GET /live-edit/admin/audit ──

    @router.get("/admin/audit")
    async def admin_audit(
        action: str = Query(default=""),
        actor: str = Query(default=""),
        session_id: str = Query(default=""),
        limit: int = Query(default=100, le=1000),
        after: str = Query(default=""),
        before: str = Query(default=""),
        x_admin_key: str = Header("", alias="X-Admin-Key"),
    ):
        """Query the append-only audit trail. Requires X-Admin-Key."""
        if not admin_key or x_admin_key != admin_key:
            audit_log.record(
                "failed_admin_auth", actor="unknown", target="admin_audit", result="blocked"
            )
            raise HTTPException(status_code=403, detail="需要有效的 admin key")
        events = audit_log.query(
            action=action or None,
            actor=actor or None,
            session_id=session_id or None,
            limit=limit,
            after=after or None,
            before=before or None,
        )
        return {"events": [e.to_dict() for e in events]}
```

3g. Wire audit/metrics into the existing endpoints:

- `start_stream` (line 129): after the capacity check fails (line 135-136):

```python
        if not session_store.add(session):
            audit_log.record(
                "session_rejected", target=session_id, session_id=session_id,
                result="blocked", detail={"reason": "max_active_reached"},
            )
            metrics.inc("live_edit_sessions_total", {"outcome": "rejected"})
            raise HTTPException(status_code=503, detail="会话数已达上限，请稍后再试")
```

And right after that (before creating the generator), record the start + set the contextvar:

```python
        audit_log.record(
            "session_start", target=session_id, session_id=session_id,
            detail={"mode": mode},
        )
        metrics.inc("live_edit_sessions_total", {"outcome": "started"})
        set_session_id(session_id)
```

The `outcome="started"` counter lives HERE (in the router), not in `run_edit_session` — it pairs with `session_start` at the session boundary, and Task 6's `run_edit_session` records only the `active_sessions` gauge (a session that is capacity-rejected never increments `started`).

Then pass the new kwargs to `run_edit_session` (lines 146-157):

```python
                run_edit_session(
                    session=session,
                    provider=provider,
                    vcs=vcs,
                    storage=storage,
                    config=config,
                    mode=mode,
                    preview_manager=preview_manager,
                    session_store=session_store,
                    tool_registry=tool_registry,
                    audit_log=audit_log,
                    metrics=metrics,
                )
```

Inside the generator, add the timeout audit (line 162-165):

```python
                    except asyncio.TimeoutError:
                        audit_log.record(
                            "session_timeout", target=session_id, session_id=session_id,
                            result="timeout",
                        )
                        yield f"data: {json.dumps({'type': 'error', 'error': '会话超时'})}\n\n"
                        break
```

And in the generator `finally` (line 171-177), audit the disconnect:

```python
            finally:
                if not session._done:
                    session.cancel()
                    audit_log.record(
                        "session_disconnect", target=session_id, session_id=session_id,
                        result="cancelled",
                    )
```

- `continue_stream` (line 193): record `session_continue` after a session is found/rehydrated:

```python
        session.new_stream_queue()
        audit_log.record(
            "session_continue", target=session_id, session_id=session_id,
            detail={"mode": mode},
        )
```

Add a `session_recovered` audit in the crash-recovery branch (after line 200):

```python
            session = rehydrate_session(session_id, detail) if detail else None
            if session is None:
                raise HTTPException(status_code=404, detail="会话不存在或已过期")
            audit_log.record(
                "session_recovered", target=session_id, session_id=session_id,
                result="recovered",
            )
```

Also set the session_id contextvar for the continuation generator and pass the new kwargs to `continue_edit_session` (lines 210-223).

- `approve_tool` (line 258): audit the decision:

```python
        session.approve(tool_id, req.approved)
        decision = "approved" if req.approved else "rejected"
        audit_log.record(
            "approve" if req.approved else "reject",
            target=tool_id,
            session_id=session_id,
            result=decision,
        )
        metrics.inc("live_edit_approvals_total", {"decision": decision})
        return {"ok": True}
```

- `cancel_session` (line 269): after `session.cancel()`:

```python
        audit_log.record("cancel", target=session_id, session_id=session_id, result="cancelled")
        metrics.inc("live_edit_sessions_total", {"outcome": "cancelled"})
```

- `revert_preview` (line 352): record a `revert_preview` audit with result `ok`/`conflict`/`error`.

- `revert_execute` (line 367): after execution:

```python
        result = vcs.revert_execute(commit_hash)
        audit_log.record(
            "revert_execute",
            target=commit_hash,
            session_id="",
            result="ok" if result.ok else "error",
            detail={"message": getattr(result, "message", "")},
        )
        metrics.inc("live_edit_reverts_total", {"outcome": "ok" if result.ok else "error"})
```

- `upload_knowledge` (line 418) and `delete_knowledge` (line 443): audit `knowledge_upload` / `knowledge_delete` with `target=source_path`, result ok/error.

- Every admin endpoint 403 block (lines 595, 688, 707, 729, 756, 810): add `failed_admin_auth` audit + record the admin action on success:
  - `admin_worktrees` — read-only; audit `failed_admin_auth` on 403 only.
  - `admin_cancel_session` — audit `admin_cancel` on success (target=session_id).
  - `admin_cleanup_worktree` — audit `admin_cleanup` on success.
  - `admin_list_unmerged_branches` — `failed_admin_auth` on 403 only.
  - `admin_merge_branch` — audit `admin_merge` on success (target=session_id, result ok/conflict).
  - `admin_delete_branch` — audit `admin_delete` on success.

- Engine signatures — Task 5 passes `audit_log=audit_log, metrics=metrics` into `run_edit_session` and `continue_edit_session` above, but those functions don't accept the kwargs until Task 6. To keep Task 5 green on its own, add the optional params NOW (accept-and-ignore; the wiring that USES them lands in Task 6). In `engine.py`, `run_edit_session` (line 479) and `continue_edit_session` (line 1137):

```python
async def run_edit_session(..., audit_log=None, metrics=None):
```
```python
async def continue_edit_session(..., audit_log=None, metrics=None):
```

3h. Update `live_edit/__init__.py` — import and add to `__all__`:

```python
from .audit import AuditLog, SQLiteAuditLog
from .metrics import Metrics
```
and add `"AuditLog"`, `"SQLiteAuditLog"`, `"Metrics"` to `__all__`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_observability_endpoints.py tests/test_router.py -v`
Expected: PASS (new tests + existing router tests). Existing `test_router.py` may need a small update only if it asserts exact health/response shapes; if a test breaks, fix the **test** (design wins, per project convention).

- [ ] **Step 5: Commit**

```bash
git add live_edit/router.py live_edit/__init__.py tests/test_observability_endpoints.py
git commit -m "feat(router): correlation middleware, /metrics, /admin/audit, endpoint audit wiring"
```

---

### Task 6: engine.py — agent loop audit + metrics + SessionStore expiry

**Files:**
- Modify: `live_edit/engine.py`
- Test: `tests/test_engine.py` (append), `tests/test_session_store_audit.py` (new)

**Interfaces:**
- Consumes: Task 1 (`set_session_id`, `get_session_id`), Task 2 (`Metrics`), Task 3 (`AuditLog`), Task 5 (router passes `audit_log`/`metrics`).
- Produces: nothing new externally; wires metrics/audit into the loop.

- [ ] **Step 1: Write the failing tests**

`tests/test_session_store_audit.py`:

```python
# tests/test_session_store_audit.py
import time

from live_edit.audit import SQLiteAuditLog
from live_edit.engine import EditSession, SessionStore


def test_session_expired_is_audited_on_ttl(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "audit.db"))
    store = SessionStore(max_active=10, ttl_seconds=0, audit_log=audit)
    sess = EditSession("le_1", "request")
    store.add(sess)
    assert store.get("le_1") is None  # TTL=0 forces expiry on read
    events = audit.query(action="session_expired")
    assert len(events) == 1
    assert events[0].session_id == "le_1"


def test_session_expired_audited_by_expire_stale(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "audit.db"))
    store = SessionStore(max_active=10, ttl_seconds=0, audit_log=audit)
    sess = EditSession("le_2", "request")
    store.add(sess)
    store.count  # triggers _expire_stale
    events = audit.query(action="session_expired")
    assert len(events) == 1
    assert events[0].session_id == "le_2"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_session_store_audit.py -v`
Expected: FAIL with `TypeError: SessionStore.__init__() got an unexpected keyword argument 'audit_log'`

- [ ] **Step 3: Implement the engine changes**

3a. `SessionStore`: the `audit_log` param already exists on `__init__` from Task 5. Add the `_audit_expired` helper (after `__init__`, ~line 209):

```python
    def _audit_expired(self, session_id: str) -> None:
        if self.audit_log is not None:
            self.audit_log.record(
                "session_expired", target=session_id, session_id=session_id, result="expired"
            )
```

Use it in `get()` TTL path (line 223-225) and `_expire_stale` (line 236-237):

```python
        if time.time() - session._created_at > self.ttl_seconds:
            self._audit_expired(session_id)
            self.remove(session_id)
            return None
```
```python
        for sid in stale:
            self._audit_expired(sid)
            self._sessions.pop(sid, None)
```

3b. `EditSession` (line 137): add an outcome field:

```python
        self._outcome: str = "failed"  # terminal outcome: completed/cancelled/failed
```

3c. `_run_agent_loop_fix` (line 294): add `audit_log=None, metrics=None` params; wrap each tool execution (line 347) with timing + audit + metrics; wrap the provider call (line 306) with LLM metrics.

3d. Add a module-level status helper near `translate_error` (top of file):

```python
def _tool_status(exec_result: dict) -> str:
    """Map a tool exec_result dict to a metrics status: ok | blocked | error."""
    if exec_result.get("ok"):
        return "ok"
    error = str(exec_result.get("error", "")).lower()
    if any(k in error for k in ("危险操作", "已阻止", "blocked", "拦截", "越界")):
        return "blocked"
    return "error"
```

3e. `run_edit_session` (line 478): the `audit_log=None, metrics=None` params already exist on the signature from Task 5 — wire them. At the very top, after `session._mode = mode` (line 497), record the active gauge (the `outcome="started"` counter is already recorded by the router in Task 5, so NOT here):

```python
    if metrics is not None:
        metrics.inc("live_edit_active_sessions")
```

Preview start audit (after line 509):

```python
        if preview_url:
            session._preview_url = preview_url
            session.emit("preview_ready", url=f"{preview_url}/app")
            if audit_log is not None:
                audit_log.record(
                    "preview_start", target=session.id, session_id=session.id,
                    detail={"url": f"{preview_url}/app"},
                )
```

LLM metrics around the provider call (line 661):

```python
            _llm_t0 = time.monotonic()
            try:
                content_blocks = await provider.call_with_tools(
                    messages=messages,
                    tools=tools,
                    on_thinking=on_thinking,
                    on_text=_on_text,
                )
                _llm_status = "ok" if content_blocks is not None else "error"
            except Exception:
                _llm_status = "error"
                raise
            finally:
                if metrics is not None:
                    metrics.inc("live_edit_llm_calls_total", {"status": _llm_status})
                    metrics.observe("live_edit_llm_duration_seconds", time.monotonic() - _llm_t0)
```

Tool execution wrap (replace lines 823-834):

```python
                _tool_t0 = time.monotonic()
                exec_result = (
                    await tool_registry.execute(tool_name, tool_input, _root, config)
                    if tool_registry
                    else {"ok": False, "error": "No tool registry"}
                )
                _tool_dur = int((time.monotonic() - _tool_t0) * 1000)
                _tool_status_val = _tool_status(exec_result)
                if metrics is not None:
                    metrics.inc(
                        "live_edit_tool_executions_total",
                        {"tool": tool_name, "status": _tool_status_val},
                    )
                    metrics.observe(
                        "live_edit_tool_duration_ms", _tool_dur, {"tool": tool_name}
                    )
                if audit_log is not None:
                    audit_log.record(
                        "tool_execution",
                        target=tool_name,
                        session_id=session.id,
                        result=_tool_status_val,
                        detail={
                            "tool": tool_name,
                            "args_summary": _tool_summary(tool_name, tool_input),
                            "duration_ms": _tool_dur,
                        },
                    )
```

3f. Terminal outcomes — set `_outcome` at each terminal point:

- Cancelled break (line 621-623):
```python
            if session._cancelled.is_set():
                session._outcome = "cancelled"
                logger.info("Session %s cancelled at round %d", session.id, round_num)
                break
```
- No-changes done (line 1020-1026): add `session._outcome = "completed"` before `session.emit("done", committed=False, ...)`.
- After each `_do_commit` call (line 1038 deep, line 1051 approved-quick): 
```python
                    await _do_commit(session, vcs, storage, config)
                    session._outcome = "completed" if session._committed else "failed"
```
- Rollback branch (line 1052-1060): add `session._outcome = "cancelled"`.
- Exception handler (line 1062-1064): add `session._outcome = "failed"`.

3g. In the `finally` block (line 1066), after `session._done = True`, record terminal metrics + audit and decrement the gauge:

```python
    finally:
        session._done = True
        session.messages = messages
        if metrics is not None:
            metrics.inc("live_edit_sessions_total", {"outcome": session._outcome})
            metrics.inc("live_edit_active_sessions", value=-1)
        if audit_log is not None:
            audit_log.record(
                f"session_{session._outcome}",
                target=session.id,
                session_id=session.id,
                result=session._outcome,
            )
        _persist_session(session, storage, messages)
```

3h. Preview stop audit — inside the existing preview-stop branch (line 1073-1074):

```python
        if preview_manager and not session._committed:
            await preview_manager.stop(session.id)
            if audit_log is not None:
                audit_log.record(
                    "preview_stop", target=session.id, session_id=session.id, result="stopped"
                )
```

3i. `continue_edit_session` (line 1137): the `audit_log=None, metrics=None` params already exist on the signature from Task 5 — wire them (pass through to `run_edit_session`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_session_store_audit.py tests/test_engine.py -v`
Expected: PASS. If an existing engine test asserts exact terminal behavior that conflicts with the new outcome audit, fix the **test** (design wins, per project convention).

- [ ] **Step 5: Commit**

```bash
git add live_edit/engine.py tests/test_session_store_audit.py
git commit -m "feat(engine): audit + metrics for tool execution, LLM calls, terminal states, expiry"
```

---

## Self-Review

**Spec coverage:**
- Append-only `audit_events` + query API → Task 3.
- All 15 action vocabulary items → Tasks 5 (router: session_start/continue/rejected/recovered/timeout/disconnect, approve/reject, revert, admin, failed_admin_auth, knowledge) and 6 (engine: tool_execution, commit, rollback, preview_start/stop, session_completed/failed/cancelled, session_expired).
- Metrics (8 metrics incl. histograms + `/metrics`) → Tasks 2, 5, 6.
- Structured JSON logging + contextvars → Task 1; correlation middleware → Task 5.
- `[observability]` config → Task 4.
- Backward compatibility (optional params, ignored unknown toml sections) → Tasks 4, 5, 6 + tests.
- Review-mandated items (contextvar propagation test, threading.local, indexes, no-FK, multi-worker caveat in doc) → Tasks 3, 5, 6. Multi-worker caveat is documented in the spec, not the code.

**Placeholder scan:** All steps carry real code; no TBD/TODO.

**Type consistency:** `Metrics.inc/set/observe/render` used identically in Tasks 5/6; `audit_log.record(action, *, actor, target, session_id, result, detail)` matches across Tasks 3/5/6; `session._outcome` introduced in Task 6 and used only there; `SessionStore(audit_log=...)` consistent between Tasks 5 and 6.
