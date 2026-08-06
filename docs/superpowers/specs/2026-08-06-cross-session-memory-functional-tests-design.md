# Cross-Session Memory Functional Tests Design

**Date**: 2026-08-06
**Status**: draft
**Scope**: a focused functional/integration test suite for the L2 long-term (cross-session / cross-user) memory feature
**Route**: new single test file `tests/test_cross_session_memory.py` (Approach A) — real `SQLiteStorage` on `tmp_path`, semantically discriminative topic-based fake embedder, explicit user A/B session namespaces

## Overview

live-edit's three-tier memory (memory.py) stores each edit session's request + per-file diffs as embedding chunks in a single project-local SQLite DB (`live_edit.db`). L2 retrieval (`LongTermMemory.retrieve_sync`, memory.py:392) searches **all** chunks in the DB with no session/user filter, then groups results by session. The intended effect — and the contract this suite locks in — is that a new session (even from a different user) automatically surfaces semantically similar past edits, without the requester explicitly referencing them.

The existing tests cover this only weakly:

- `tests/test_long_term.py` uses a **constant-vector FakeEmbedder** (memory.py test doubles, test_long_term.py:17-28) where cosine similarity between any two texts is exactly 1.0 — it cannot distinguish a relevant past edit from an unrelated one, so "semantic recall" is never really exercised.
- Storage-level persistence, migration, and eviction **are** covered with real `SQLiteStorage` in `tests/test_storage.py` and `tests/test_session_memory.py::TestMigration` — but there is **no integration coverage of the L2 `LongTermMemory` layer itself**: no store→retrieve round-trip through memory.py on a real DB exercising its scoring and brute-force/vec fallback logic.
- No test explicitly models the "user B automatically borrows user A" scenario the feature is meant to deliver.

This spec adds one new functional test file that closes those gaps.

## Non-Goals

- No engine/HTTP end-to-end tests (no `engine.run_edit_session`, no router/TestClient).
- No coverage of the legacy `session_memory.SessionMemory` class (kept for backward compat; the current engine path is `MemoryManager → LongTermMemory`).
- No real-model (sentence-transformers) download in CI. Real-model eval stays a manual opt-in (`tests/test_rag_eval.py::run_real_eval`).
- No production code changes. This is a test-only addition.

## Architecture

```
tests/test_cross_session_memory.py   (new)
├── TopicFakeEmbedder                 semantically discriminative, deterministic
├── fixtures                          storage(tmp_path), topic_embedder, ltm(config)
└── 10 test groups / ~35 cases
```

### TopicFakeEmbedder (test-local asset)

Deterministic embeddings that let cosine similarity actually separate "similar" from "unrelated", fixing the constant-vector weakness:

- A fixed set of topics, each mapped to an orthogonal basis direction vector (dimension configurable, default 8):
  `auth, bugfix, db, style, docs, ratelimit` (same topic set as `tests/test_rag_eval.py`; **deliberate deviation** — unknown text maps to an independent `other` vector, not test_rag_eval.py:259's fallback-to-`auth`).
- `embed(text)`: classify text by keyword matches into a topic → return that topic's direction vector, with a small per-text deterministic perturbation (seeded by text hash) so different texts on the same topic still have cosine slightly < 1.0.
- Text with no topic keyword maps to a distinct "other" vector orthogonal to all topics → retrieves nothing against real topics.
- `embed_batch` delegates to `embed`; `dimension` returns the configured dim.

Guarantees: same input → same vector (hash-seeded); same topic → high cosine (~0.9+); different topics → cosine ≈ 0; `other` → cosine ≈ 0 vs any topic.

### Storage fixture

- `storage(tmp_path)`: real `SQLiteStorage` at `tmp_path / "test_cross_session.db"`.
- Exercises real table creation, `session_chunks` persistence, vec sync (`session_chunks_vec`) when sqlite-vec is installed, and automatic brute-force fallback when it is not.
- **Path caveats** — the default CI path is brute-force (sqlite-vec is optional and not installed in the dev env); where behavior differs between the two paths, tests must pin a path explicitly:
  - Re-storing a session only DELETEs parent `session_chunks` rows; old `session_chunks_vec` rowids become orphans (storage.py:286-294). **Chunk-count assertions must query the parent table**, never the vec table.
  - Malformed-row-skip holds deterministically only under brute-force; tests relying on it must force brute-force (monkeypatch `query_chunks_vec → None`).
  - The vec path is additionally truncated by `coarse_recall_limit` (default 200, config.py:156); irrelevant for this suite's small corpus.

### User A / B simulation

- Session IDs use distinct namespaces: `user_a:<uuid-hex>` / `user_b:<uuid-hex>`.
- The suite proves B's retrieval returns A's chunks — i.e. the current no-user-scoping contract — and that retrieval is cross-session by design.

### Configuration

- Tests build `LongTermConfig` explicitly per scenario (threshold / decay / hit-weight / max-entries). No dependence on config-file loading.
- `memory.enabled=False` case uses `MemoryConfig(enabled=False)` through `MemoryManager` to verify the master switch.

## Test Matrix

| # | Group | Validates | Key assertions |
|---|-------|-----------|----------------|
| 1 | Main path (cross-user recall) | B's new session auto-recalls A's similar edit, without explicit reference; same-topic edits from A and B coexist in one DB and B's query surfaces **both** (strongest no-isolation proof) | threshold pinned to default 0.6 (>0, so cross-topic≈0 is filtered — "unrelated → empty" only holds with threshold > 0); retrieved `session_id`s include `user_a:…` and `user_b:…`; differently-worded query sharing a topic keyword (A stored "implement token-based auth for login", B queries "add JWT authentication" — both hit `auth`) still hits; unrelated query → empty |
| 2 | Cross-user visibility & topic filtering | A and B store different topics in one DB; B's query recalls only relevant topic, across users | no wrong-topic entries in results; relevant topic present |
| 3 | Scoring behavior | below-threshold filtering; recency decay lowers old scores; hit-count bonus + 10 cap; `max_entries` truncation; per-session top-2 grouping; `file_diff` retained over `request` | cross-topic (cosine≈0) not recalled — **no exact-0.6 boundary construction** (hash perturbation makes it non-constructible); old chunk score significantly lower (decay test writes old `last_accessed` via SQL); hit bonus within cap; per-session ≤2; **`file_diff`-vs-`request` inferred via non-empty `file_path`** (MemoryEntry has no `chunk_type`) — store 1 request + 2 file_diff and assert the 2 retained are the file_diffs (global re-sort at memory.py:568 by bare score hides the −0.05 tiebreak otherwise); **reset/isolate hit counts between assertions** (retrieve triggers `update_chunk_hit_counts`, drifting scores) |
| 4 | Continuation semantics | re-store same session atomically replaces old chunks (no duplication); continuation recalls its own history | **parent-table** chunk count unchanged after re-store — the only assertion valid in the no-vec CI env (vec orphan-row behavior exists only when sqlite-vec is installed; guard any vec assertion with `pytest.skipif` on vec absence); own session retrievable |
| 5 | Store behavior | 1 request chunk + N file_diff chunks per modified file; empty diff → request-only; binary diff skipped | chunk_type counts exact; **store is async** (`asyncio.run` or `MemoryManager.store_sync`); diffs must match `_split_diff_by_file` parsing (`+++ b/`, `Binary files `, empty); **rename chunks take the `from` path as `file_path` — use non-rename diffs** |
| 6 | Context formatting | `_format_memory_context` fields, "reference only" disclaimer, score clamped ≤100% | output contains disclaimer + fields; percent ≤ 100%; uses the default branch (requires `memory_prompt_template=""` — the default) |
| 7 | Eviction | `max_stored_entries` evicts oldest sessions | evicted session's chunks gone, newest retained; eviction runs after **every** store and sorts by `MIN(created_at)` (second-granular) — **`sleep(1.1)` after every store** to avoid same-second ties (not just before the newest) |
| 8 | Disabled switch | `MemoryConfig(enabled=False)` master switch → store & retrieve no-op | no chunks written to `session_chunks`; `MemoryManager.retrieve` returns `("", messages)` (empty context, unchanged messages) — **use the master switch**, not `LongTermConfig.enabled` alone (two distinct switches) |
| 9 | Robustness | malformed embedding row skipped (not crash); empty DB; brute-force fallback | **force brute-force** (monkeypatch `query_chunks_vec → None`); malformed row skipped and others still returned — holds only when the malformed row entered via `store_chunks` (its single per-session vec INSERT is atomic, so the vec path silently drops the whole session; that path is untestable without sqlite-vec); empty query / empty DB safe |
| 10 | Retrieval side effect | retrieve bumps `hit_count` and refreshes `last_accessed` on matched chunks (via `update_chunk_hit_counts`, storage.py:607) | after a retrieve, matched chunk's `hit_count` incremented and `last_accessed` non-null (read back via `query_chunks`) |

## Error Handling & Robustness

- Malformed/stale-dimension embedding rows must be skipped without failing the whole query (mirrors memory.py:483-485).
- Empty DB and empty query return `[]` cleanly.
- sqlite-vec absence is normal (optional dep); retrieval must fall back to brute-force and still pass.
- Store failure is non-fatal by design (memory.py:380-385 logs a warning); tests assert the graceful path, not exceptions.

## Success Criteria

- New suite passes in CI with no model download; deterministic, no network.
- No conflicts with existing tests; new topic-based embedder is additive.
- Line coverage of `live_edit/memory.py` reaches **≥ 60%** (the repo's own `fail_under`) when the new suite runs alongside the existing memory tests — current baseline with existing memory tests alone is **42%** (measured 2026-08-06), so the suite must add the L2 branches it targets. Verified via `pytest --cov=live_edit.memory --cov-report=term-missing`.
- Full suite (`pytest`) stays green.

## Implementation Approach (per repo CLAUDE.md dual-agent mode)

1. Split into 2-3 TaskCreate items (fixtures/embedder scaffold → core cross-user matrix → scoring/robustness matrix).
2. Dispatch subagents to implement the test file (full context given, no self-reading).
3. After each task: parallel Spec review + Code-quality review by subagents.
4. Fix loop via subagents on FAIL; never fix by hand.
5. Run full `pytest` + `pytest --cov=live_edit.memory` before delivery.
