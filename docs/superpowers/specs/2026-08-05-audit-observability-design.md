# Audit Logging & Observability Design

**Date**: 2026-08-05
**Status**: draft
**Scope**: append-only audit trail + structured JSON logging + process-local metrics
**Route**: self-contained lightweight (Approach A) — no OpenTelemetry / external observability stack

## Overview

live-edit currently has no first-class audit trail and no observability: only scattered `logging.getLogger("live-edit.*")` calls (storage.py:11, engine.py:18, provider.py:11, vcs.py:11, router.py:30) with no unified format, no structured fields, no metrics, and no tracing. The `/health` endpoint returns only `active_sessions` count (router.py:408).

This spec adds three capabilities, all self-contained, all best-effort (never break the main agent flow):

1. **Audit log** — append-only `audit_events` table in SQLite, queryable via an admin endpoint. Records WHO (actor) did WHAT (action) to WHAT (target), when, and with what result.
2. **Structured JSON logging** — a `JsonFormatter` for the `live-edit.*` logger namespace, with `session_id` / `correlation_id` injected via contextvars. Existing ad-hoc log calls become structured automatically.
3. **Metrics** — process-local, thread-safe counters/gauges/histograms exposed at `GET /live-edit/metrics` in Prometheus text format, no new dependencies.

## Architecture

```
live_edit/
├── audit.py      (new)  AuditEvent, AuditLog, SQLiteAuditLog
├── metrics.py    (new)  Metrics registry (counter/gauge/histogram) + Prometheus render
├── logging.py    (new)  JsonFormatter, configure_logging(), contextvar setup
├── config.py            + ObservabilityConfig, [observability] section parser
├── router.py            wire audit/metrics into endpoints + correlation middleware + new endpoints
└── engine.py            wire audit/metrics into agent loop, tool execution, commit, terminal states
```

All three are injectable/pluggable, mirroring the existing Provider / Storage / VCS pattern:

- `setup_live_edit(audit_log=..., metrics=...)` — new optional params, backward compatible.
- Defaults: `SQLiteAuditLog(db_path)` and a module-level `Metrics()` instance.

## 1. Audit Log

### Event model

```
AuditEvent:
  id           int    AUTOINCREMENT primary key
  ts           str    ISO-8601 UTC
  actor        str    "anonymous" | "admin" (until RBAC lands; see §Security)
  action       str    controlled vocabulary (see §Actions)
  target       str    session_id / commit_hash / branch / path / tool name
  session_id   str    "" when not session-scoped
  result       str    ok | failed | blocked | rejected | timeout | ...
  detail_json  str    JSON object, top-level scalar keys only
```

### Storage: SQLiteAuditLog

- Table `audit_events`, **INSERT only** — no UPDATE/DELETE methods on the public interface. AUTOINCREMENT id gives monotonicity.
- Shares the same `live_edit.db` as `SQLiteStorage`; **own per-thread connection** via `threading.local` (mirrors storage.py:55,59-73), same WAL pragma.
- Default db path aligns with router.py:96 (`os.path.join(project_root, "live_edit.db")`).
- **No foreign keys** — `live_edit_sessions` uses `INSERT OR REPLACE` (storage.py:163), so an FK would break/require cascade handling.
- **Indexes**: `(action, ts)`, `(session_id)`, `(actor)` to keep time-range filtering fast.

### Interface

```
record(action, *, actor="anonymous", target="", session_id="", result="ok", detail=None) -> int
query(*, action=None, actor=None, session_id=None, limit=100, after=None, before=None) -> list[AuditEvent]
```

- Writes are **best-effort**: any failure logs `logger.warning` and returns, never raising into the caller. Governance is a record, not a gate.
- `record()` is synchronous and may block the event loop briefly (single INSERT); document this. V2 could move to an async queue if needed.

### Actions (controlled vocabulary)

| action | trigger |
|--------|---------|
| `session_start` | POST /stream (router.py:129) |
| `session_continue` | POST /continue/{id} (router.py:193) |
| `session_rejected` | capacity 503 (router.py:135-136, 203-204) |
| `session_recovered` | crash recovery rehydrate (router.py:199-201) |
| `session_timeout` | SSE read timeout (router.py:162-165) |
| `session_disconnect` | client disconnect cancels session (router.py:171-177, 237-242) |
| `session_completed` / `session_failed` / `session_cancelled` | terminal state, unified landing in the session `finally` (engine.py:1066-1082) |
| `session_expired` | SessionStore TTL eviction (engine.py:223-225, 232-237) |
| `tool_execution` | each tool run: main loop (engine.py:757-846) **and** `_run_agent_loop_fix` (engine.py:347-348); detail: `{tool, args_summary, duration_ms}`; result ok/error/blocked |
| `approve` / `reject` | POST /approve (router.py:258-265) and engine timeout path → `timeout` |
| `commit` | `_do_commit` (engine.py:390-475); result ok/failed; target = commit_hash |
| `rollback` | terminal reject discards changes (engine.py:1052-1060) |
| `revert_preview` | POST /revert/{hash}/preview (router.py:352) |
| `revert_execute` | POST /revert/{hash}/execute (router.py:367) |
| `preview_start` / `preview_stop` | per-session preview lifecycle (engine.py:507-510, 1073-1074) |
| `failed_admin_auth` | any admin endpoint 403 (router.py:595, 688, 707, 729, 756, 810) |
| `admin_merge` / `admin_delete` / `admin_cleanup` / `admin_cancel` | admin branch/worktree operations (router.py:682-824) |
| `knowledge_upload` / `knowledge_delete` | knowledge base API (router.py:418-457) |

## 2. Metrics

Process-local, thread-safe registry (a `lock` guarding dicts). Interface:

```
inc(name, labels=dict)          # counters
set(name, labels=dict, value)   # gauges
observe(name, value, labels)    # histograms, fixed buckets
render() -> str                 # Prometheus text exposition format
```

### Metric set

| metric | type | labels |
|--------|------|--------|
| `live_edit_sessions_total` | counter | outcome=started\|completed\|failed\|cancelled\|rejected |
| `live_edit_active_sessions` | gauge | — |
| `live_edit_llm_calls_total` | counter | status=ok\|error\|timeout |
| `live_edit_llm_duration_seconds` | histogram | — |
| `live_edit_tool_executions_total` | counter | tool, status=ok\|error\|blocked |
| `live_edit_approvals_total` | counter | decision=approved\|rejected\|timeout |
| `live_edit_reverts_total` | counter | outcome=ok\|conflict\|error |
| `live_edit_errors_total` | counter | error_type |

Fixed histogram buckets: `0.005 0.01 0.025 0.05 0.1 0.25 0.5 1 2.5 5 10 30 60 120`.

### Multi-worker caveat

Metrics are **per-process**. Under uvicorn multi-worker deployment each worker exposes its own partial counters; the audit log (SQLite) is shared. Document this: for consistent metrics run a single worker, or label `/metrics` with the process pid. This is a documented limitation, not a v1 blocker.

## 3. Structured Logging

- `JsonFormatter` emits one JSON line per record: `{ts, level, logger, message, session_id, correlation_id, ...extra}`.
- `session_id` and `correlation_id` come from contextvars (`live_edit.session_id`, `live_edit.correlation_id`). The formatter reads them, so **all existing `logger.info/warning/error` calls across the codebase become structured with zero per-callsite changes**.
- `configure_logging(level=..., json=True, stream=...)` is exposed for embedders. `setup_live_edit` applies the `[observability]` config **only to the `live-edit.*` logger namespace**, never touching the root logger (a library must not hijack the host app's logging).
- Correlation: an `@router.middleware("http")` reads `X-Request-ID` or generates one, sets the `correlation_id` contextvar, and echoes it in the response header. For SSE streams the `session_id` becomes the anchor.

### contextvar propagation (critical)

`asyncio.ensure_future` (router.py:145,210) copies the current context at call time. Therefore:

- The `session_id` contextvar must be **set before** `run_edit_session` / `continue_edit_session` is scheduled, so the whole background loop carries it.
- Audit calls that happen inside the SSE `event_generator` (disconnect, timeout paths) and inside `approve`/`cancel` endpoints must set the session_id contextvar themselves (or take an explicit `session_id=` arg to `record()`).
- An integration test must lock in that the contextvar reaches the background task and does not leak across sessions.

## 4. API Endpoints

| method | path | auth | notes |
|--------|------|------|-------|
| GET | `/live-edit/metrics` | none by default | Prometheus text; host reverse-proxy may gate it (§YAGNI: no built-in `metrics_require_admin` toggle) |
| GET | `/live-edit/admin/audit` | `X-Admin-Key` | filters: `action`, `actor`, `session_id`, `limit`, `after`, `before` |

## 5. Configuration

```toml
[observability]
log_level = "INFO"
json_logs = true
metrics_enabled = true
audit_enabled = true
```

`ObservabilityConfig` dataclass + parser in config.py. Backward compatible: `parse_config` uses `raw.get()` throughout (config.py:289-458) so unknown sections/keys in existing `.live-edit.toml` files are ignored; the new section is purely additive. Validation (config.py:464-482) is untouched.

## 6. Error Handling & Guarantees

- Audit and metric writes are **best-effort**: failures log and are swallowed; they never raise into or interrupt the agent loop, SSE stream, or API handler.
- Audit `record()` is synchronous; note the blocking cost of one INSERT per event in the docs.
- `SQLiteAuditLog` is thread-safe via `threading.local` per-thread connections (same pattern as `SQLiteStorage`).

## 7. Security Considerations

- **Actor identity is provisional**: until RBAC exists, audit `actor` records `"anonymous"` for session actions and `"admin"` for admin-endpoint actions. This is an honest cut, upgraded when user identity lands (see the enterprise roadmap: RBAC is the next gap).
- **Integrity**: v1 relies on INSERT-only + no delete API + monotonic AUTOINCREMENT. A tamper-evident hash chain (each row includes the previous row's hash) is **deferred to v2** and only when a real compliance requirement demands it.
- `/metrics` is unauthenticated by default (Prometheus scrape convention); gate at the reverse proxy for production.
- `failed_admin_auth` events make admin-key brute-force attempts visible in the audit trail.

## 8. Testing Plan

New files: `tests/test_audit.py`, `tests/test_metrics.py`, `tests/test_logging.py`; integration additions in `tests/test_router.py` / `tests/test_engine.py`.

Must-cover cases (from design review):

1. **contextvar propagation/isolation into the background agent task** — the most critical test; verify session_id reaches `run_edit_session` and does not leak across concurrent sessions.
2. Audit: append-only (no update/delete paths), every action in §Actions fires on its trigger, write-failure is best-effort and does not raise.
3. Terminal-state audit: disconnect→cancel, SSE timeout→timeout, exception→failed, capacity→rejected, fix-loop tool counts.
4. Metrics: counter/gauge/histogram semantics, thread-safety under concurrency, `/metrics` Prometheus text format.
5. Logging: JSON line format, contextvar fields present, old toml with/without `[observability]` parses identically.
6. Endpoint triggers: `/admin/audit` filters + auth; admin 403 emits `failed_admin_auth`.

## Open Decisions / Deferred

- v2: audit hash-chain integrity; async audit writer; OTel exporter as a swap-in implementation of the `AuditLog`/`Metrics` interfaces.
- Explicitly cut (YAGNI): `metrics_require_admin` toggle, audit `count()` unless tests need it, `detail_json` beyond top-level scalar keys.
