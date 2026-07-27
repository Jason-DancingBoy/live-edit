# RAG Session Memory — Per-File Chunking Upgrade

**Date:** 2026-07-28
**Status:** draft
**Replaces:** 2026-07-05-rag-session-memory-design.md (v1 → v2)

## 1. Motivation

v1 embeds each session as a single vector (request text only). On retrieval, the full
session summary is injected into the system prompt. Two problems:

1. **Context bloat**: a session touching 5 files injects all 5 files + request, even
   when only 1 file's change is relevant.
2. **Poor precision**: a "refactor auth" query pulls in CSS changes from the same
   historical session because the single vector can't distinguish files.

v2 chunks each session into per-file embeddings, retrieves at file granularity, and
injects only the relevant file diff — cutting injected token volume by 60-80%.

## 2. Architecture

```
store()                                   retrieve()

request ──→ request_chunk ──→ embed ──→ DB   query ──→ embed
               │                                   │
diff ──→ split_by_file()                          ├─→ cosine × chunks
  ├─ a.py stat ──→ file_chunk_1 ──→ embed ──→ DB   │
  ├─ b.py stat ──→ file_chunk_2 ──→ embed ──→ DB   └─→ top-K → group by session
  └─ c.py stat ──→ file_chunk_3 ──→ embed ──→ DB        → inject compact format
```

**Key changes from v1:**
- `store()` accepts `diff: str` (full `git diff --cached` output)
- Each commit writes 1 request chunk + N file_diff chunks (one per modified file)
- `retrieve()` returns deduplicated per-session results (2 chunks/session max)
- Eviction is session-level, not row-level
- Migration from old `session_embeddings` table is idempotent

## 3. Schema

```sql
-- New table; old session_embeddings retained read-only for migration
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
);
CREATE INDEX IF NOT EXISTS idx_chunks_session ON session_chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_type ON session_chunks(chunk_type);
```

**Migration tracking** — use SQLite `PRAGMA user_version`:
```sql
PRAGMA user_version;  -- 0 = pre-chunking, 1 = migrated
```

## 4. Chunk Text Design (FIXED per review)

**Problem:** v1 draft embedded raw diff syntax (`@@ -1,5 +1,6 @@`, `+import sys`).
`all-MiniLM-L6-v2` is trained on natural language, not code diffs — diff headers
produce near-random vectors.

**Fix:** Use `diff stat` (file path + line counts) which is natural-language-like:

```python
# request chunk
chunk_text = request

# file_diff chunk — semantic signal without diff syntax noise
stat = f"+{lines_added}/-{lines_removed}"  # e.g. "+42/-8"
chunk_text = f"{request}\nFile: {file_path}\nChanges: {stat}"

payload_json = json.dumps({
    "file": file_path,
    "diff": diff_content[:3000],   # truncated to prevent DB bloat
    "stat": stat,
    "request": request,
    "commit_hash": commit_hash,
})
```

**Rationale:** `"add JWT login\nFile: src/auth.py\nChanges: +42/-8"` is a semantic
sentence. The model can distinguish this from `"add JWT login\nFile: src/login.css\nChanges: +15/-3"`.

## 5. Chunking & Store Logic

```
store(session_id, request, files, diff, commit_hash):
  1. if not enabled: return
  2. DELETE FROM session_chunks WHERE session_id = ?   -- cleanup old chunks (continuation safety)
  3. embed_batch([request] + per_file_chunk_texts)     -- single batch inference
  4. BEGIN TRANSACTION
  5. INSERT request chunk
  6. INSERT N file_diff chunks
  7. COMMIT
  8. evict if session_count > max_stored_entries
  -- steps 2-8 all inside ONE run_in_executor call for atomicity
```

**Diff parsing — `_split_diff_by_file()`:**
- Split on `^diff --git ` boundaries
- Skip entries matching `^Binary files` (binary files — store stat-only chunk with `diff: null`)
- Skip entries matching `^rename from` / `^rename to` (treat as file_diff with 0/0 stat)
- Parse `--- a/...` / `+++ b/...` headers to extract file path
- Count `+` and `-` prefixed lines for stat
- If no file_diff chunks produced (empty diff, all binary): store only request chunk

## 6. Retrieval Logic

```
retrieve(request):
  1. embed(request) → query_vec
  2. SELECT id, session_id, commit_hash, chunk_type, chunk_text, payload_json,
            file_path, embedding
     FROM session_chunks
     ORDER BY created_at DESC
     LIMIT 15000  -- hard cap to bound scan time
  3. for each chunk: compute cosine(query_vec, embedding)
  4. keep only chunks with score >= similarity_threshold
  5. group by session_id:
       - per session: keep top-2 chunks, prefer file_diff over request
         (if file_diff and request have scores within 0.1, keep file_diff;
          if only 1 chunk, keep just that)
       - session_score = max(chunk scores)
  6. sort sessions by session_score DESC, take top max_entries sessions
  7. format and return
```

**Why top-2 per session (not 1):** If a user asks "how were auth and login changed
together?", two file_diff chunks from the same session both carry relevant signal.
Capping at 2 prevents bloat while preserving cross-file context. Review flagged
top-1 as information loss.

**Why prefer file_diff:** request chunks carry no diff. A query matching the request
text exactly but requiring actual code context gets more value from the file_diff.

## 7. Eviction — Session-Level

```sql
-- Keep max_stored_entries most recent sessions, delete the rest atomically
DELETE FROM session_chunks
WHERE session_id IN (
    SELECT session_id FROM (
        SELECT DISTINCT session_id, MIN(created_at) AS first_seen
        FROM session_chunks
        GROUP BY session_id
        ORDER BY first_seen DESC
        LIMIT -1 OFFSET ?
    )
);
```

This deletes ALL chunks belonging to evicted sessions — no fragmentation.

## 8. Injection Format

```python
def _format_memory_context(entries: list[MemoryEntry], template: str = "") -> str:
    """Compact per-file format. ~50-80 tokens per entry vs ~200-300 in v1."""
    if template:
        # supports {request}, {file}, {diff_summary}, {commit_hash}, {score}, {stat}
        ...

    lines = [
        "## Relevant Past Changes",
        "Similar past edits (reference only, adapt to current request):",
        "",
    ]
    for i, entry in enumerate(entries, 1):
        lines.append(f'{i}. "{entry.request}" ({entry.score:.0%})')
        lines.append(f"   → {entry.file_path}: {entry.diff_summary}")
    return "\n".join(lines)

# diff_summary = first 4 lines of diff content (hunk header + context), ~60 chars
```

**Token comparison (estimated):**

| | v1 (per session) | v2 (per file chunk) |
|---|---|---|
| Per entry | ~200-300 tokens | ~50-80 tokens |
| 3 results | ~600-900 tokens | ~150-250 tokens |
| Reduction | — | **60-75%** |

## 9. Migration

Run once at `SessionMemory.__init__()` time:

```python
def _migrate_if_needed(self):
    conn = self._storage._get_conn()
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

    # Idempotent: INSERT OR IGNORE on (session_id, chunk_type) unique pair
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_migration
        ON session_chunks(session_id, chunk_type)
    """)

    conn.execute("""
        INSERT OR IGNORE INTO session_chunks
            (session_id, chunk_type, chunk_text, payload_json, embedding, created_at)
        SELECT
            session_id,
            'request',
            request,
            json_object('request', request, 'files', files_json, 'migrated', json('true')),
            embedding,
            created_at
        FROM session_embeddings
    """)

    conn.execute("DROP INDEX IF EXISTS idx_chunks_migration")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    # Old table kept as audit trail, not dropped
```

**Safety properties:**
- `PRAGMA user_version` ensures run-once, even after crash
- `INSERT OR IGNORE` + temp unique index on `(session_id, chunk_type)` makes re-runs safe
- Crash mid-migration: next startup re-runs, duplicates skipped by IGNORE
- Migrated rows get `chunk_type='request'` only (no file_diff — diff not available from v1 data)
- Old `session_embeddings` table retained read-only

## 10. Edge Cases

| Scenario | Handling |
|---|---|
| Empty diff (no files modified) | `_do_commit` already skips store when `modified_files` is empty |
| Binary file diff | `_split_diff_by_file` skips binary entries; others still chunked |
| Rename-only file | Stat `+0/-0`, still produces a chunk with file path signal |
| Diff > 3000 chars | `payload_json.diff` truncated to 3000 chars; full diff not needed for injection |
| Continuation session (same id, new commit) | `store()` DELETEs old chunks by session_id before inserting new ones |
| Concurrent store + retrieve | SQLite WAL: write in single transaction, read snapshot-isolated |
| Migration crash mid-way | `PRAGMA user_version` stays 0 → re-runs; `INSERT OR IGNORE` skips dupes |
| No old data to migrate | `user_version` set to 1 immediately, no-op |
| Chunk table grows > 15000 rows | `query_embeddings` LIMIT 15000 bounds scan; session eviction keeps count in check |

## 11. Config

No changes to `SessionMemoryConfig` or `EmbedderConfig`. Existing fields suffice:
- `max_entries`: top-K sessions returned (each session contributes up to 2 chunks)
- `similarity_threshold`: unchanged
- `max_stored_entries`: now counts sessions, not rows
- `memory_prompt_template`: updated placeholder set `{request} {file} {diff_summary} {stat} {commit_hash} {score}`

## 12. Files Changed

| File | Change | Effort |
|---|---|---|
| `live_edit/storage.py` | New table `session_chunks`, methods `store_chunks()` `query_chunks()` `delete_old_sessions()` `get_db_version()` `set_db_version()` | Medium |
| `live_edit/session_memory.py` | Rewrite `store()` `retrieve()`; add `_split_diff_by_file()` `_migrate_if_needed()`; update `MemoryEntry` with `file_path` `diff_summary` `stat`; remove old `_evict_if_needed()` | Heavy |
| `live_edit/engine.py` | `_do_commit()` pass `diff=session._cached_diff, commit_hash=session._commit_hash`; `_format_memory_context()` new compact template; `run_edit_session()` single SessionMemory init | Small |
| `live_edit/embedder.py` | No change | — |
| `live_edit/config.py` | No change | — |
| `tests/test_session_memory.py` | Rewrite for chunk semantics | Medium |
| `tests/test_storage.py` | Add chunk table + migration tests | Small |
| `tests/test_engine.py` | Update `_format_memory_context` tests | Small |

## 13. Review Fixes Applied

| Review finding | Fix |
|---|---|
| C3: diff syntax in embedding produces noise | Use `stat` (+N/-M) instead of raw diff lines in `chunk_text` |
| C1: 1+N chunk writes not atomic | Single `run_in_executor` with BEGIN/COMMIT transaction |
| C2: migration not idempotent | `PRAGMA user_version` + `INSERT OR IGNORE` + temp unique index |
| Spec #6: chunk-level eviction fragments sessions | Session-level eviction (DELETE by session_id group) |
| Spec #2e: continuation sessions duplicate chunks | `store()` DELETEs existing chunks by session_id first |
| Spec #4 / I1: top-1 dedup loses cross-file context | Top-2 per session, prefer file_diff |
| I2: full scan no LIMIT | `LIMIT 15000` hard cap on query |
| I3: Storage ABC interface gap | Keep methods concrete on SQLiteStorage; SessionMemory accesses via duck-typing (same pattern as current `query_embeddings`) |
| M1: binary/rename/empty diffs | Explicit skip/stat-only handling in `_split_diff_by_file` |
| M2: LIMIT -1 SQLite compat | Documented as SQLite ≥ 3.25 requirement (Ubuntu 20.04+ default) |
| Spec #7: payload JSON bloat | Truncate diff to 3000 chars in payload_json |
| —: embed_batch underused | Single `embed_batch()` call for all chunks in store() |
