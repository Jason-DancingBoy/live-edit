# Three-Tier Memory System Design

**Date**: 2026-08-03
**Status**: approved
**Scope**: L1 short-term, L2 long-term (replaces SessionMemory), L3 knowledge base RAG

## Overview

Replace the current flat `SessionMemory` with a three-tier memory architecture managed by a unified `MemoryManager`. Each tier addresses a different timescale and retrieval strategy:

| Tier | Scope | Storage | Retrieval |
|------|-------|---------|-----------|
| L1 Short-term | Current session, last N rounds | In-memory `messages` | Window truncation + summarization |
| L2 Long-term | Cross-session edit history | SQLite `session_chunks` + sqlite-vec | Semantic search + recency decay + hit count |
| L3 Knowledge | Project docs, independent of sessions | SQLite `knowledge_chunks` + sqlite-vec | Fallback when L2 returns empty |

## Architecture

```
MemoryManager
├── ShortTermMemory     (L1)
├── LongTermMemory      (L2, refactored from SessionMemory)
└── KnowledgeBase       (L3, new)
```

### Retrieval Flow

```
retrieve(query, session_id, messages, round_num)
│
├─ L1: ShortTermMemory.manage(messages, round_num)
│     Returns (mutated_messages, summary_text)
│
├─ L2: LongTermMemory.retrieve(query)
│     sqlite-vec coarse → Python fine-rank → list[MemoryEntry]
│
├─ L3: if L2 returned empty AND knowledge.enabled:
│     KnowledgeBase.search(query) → list[KnowledgeEntry]
│
└─ Concatenate context string, return
```

### Store Flow

```
store(session_id, request, files, diff, commit_hash)
│
└─ L2: LongTermMemory.store(...)
      Parse diff → chunk by file → batch embed → write session_chunks + vec
```

L1 and L3 have no store path — L1 is ephemeral, L3 is managed via file sync and API.

## Configuration

### .live-edit.toml

```toml
[memory]
enabled = false                 # master switch; false disables all tiers

[memory.short_term]
enabled = true
max_full_rounds = 3             # keep full messages for last N rounds
max_stripped_rounds = 7         # store tool_name + result summary for older rounds
max_summary_rounds = 20         # summarize only when max_stripped_rounds < round_num <= this; beyond this, strip only
summary_model = ""              # empty = reuse llm.model

[memory.long_term]
enabled = false
embedder = { type = "local", model = "thenlper/gte-small" }
max_entries = 10
similarity_threshold = 0.6
max_stored_entries = 5000
recency_decay_rate = 0.01       # exp(-rate × days_since_last_access)
hit_count_weight = 0.05         # bonus per historical hit (capped at 10 hits)
coarse_recall_limit = 200
memory_prompt_template = ""

[memory.knowledge]
enabled = false
api_enabled = false
knowledge_dir = ".live-edit/knowledge"   # relative to project root (where .live-edit.toml lives)
chunk_size = 500
chunk_overlap = 50
max_entries = 20
# L3 triggers when L2 returns empty (no chunk passes similarity_threshold)
```

### Python Dataclasses

```python
@dataclass
class ShortTermConfig:
    enabled: bool = True
    max_full_rounds: int = 3
    max_stripped_rounds: int = 7
    max_summary_rounds: int = 20
    summary_model: str = ""

    def __post_init__(self):
        if self.max_stripped_rounds < self.max_full_rounds:
            raise ValueError("max_stripped_rounds must be >= max_full_rounds")
        if self.max_summary_rounds < self.max_stripped_rounds:
            raise ValueError("max_summary_rounds must be >= max_stripped_rounds")

@dataclass
class LongTermConfig:
    enabled: bool = False
    max_entries: int = 10
    similarity_threshold: float = 0.6
    max_stored_entries: int = 5000
    recency_decay_rate: float = 0.01      # exp(-rate × days)
    hit_count_weight: float = 0.05
    coarse_recall_limit: int = 200
    memory_prompt_template: str = ""
    embedder: EmbedderConfig = field(default_factory=EmbedderConfig)

    def __post_init__(self):
        if not (0 <= self.similarity_threshold <= 1):
            raise ValueError("similarity_threshold must be in [0, 1]")
        if not (0 <= self.recency_decay_rate <= 1):
            raise ValueError("recency_decay_rate must be in [0, 1]")
        if not (0 <= self.hit_count_weight <= 1):
            raise ValueError("hit_count_weight must be in [0, 1]")
        if self.coarse_recall_limit < 1:
            raise ValueError("coarse_recall_limit must be >= 1")
        if self.max_stored_entries < 1:
            raise ValueError("max_stored_entries must be >= 1")

@dataclass
class KnowledgeConfig:
    enabled: bool = False
    api_enabled: bool = False
    knowledge_dir: str = ".live-edit/knowledge"
    chunk_size: int = 500
    chunk_overlap: int = 50
    max_entries: int = 20

    def __post_init__(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be < chunk_size")

@dataclass
class MemoryConfig:
    enabled: bool = False       # master switch: false disables all tiers
    short_term: ShortTermConfig = field(default_factory=ShortTermConfig)
    long_term: LongTermConfig = field(default_factory=LongTermConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)

# Backward-compatible alias
SessionMemoryConfig = LongTermConfig  # deprecated
```

### Backward Compatibility

1. **TOML layer**: Both `[memory.long_term]` and `[session_memory]` are accepted. `[memory.long_term]` takes priority. Detection uses key existence, not default-value comparison.
2. **Python attribute**: `Config` retains `session_memory` property, getter delegates to `self.memory.long_term`. Existing `config.session_memory.enabled` access continues to work.
3. **Class name**: `SessionMemoryConfig = LongTermConfig` alias at module level prevents `ImportError` on existing imports.
4. **Default behavior preserved**: `memory.enabled = False` and `long_term.enabled = False` match the current opt-in default.

## Storage Schema

### session_chunks (modified)

```sql
-- New columns
ALTER TABLE session_chunks ADD COLUMN hit_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE session_chunks ADD COLUMN last_accessed TEXT;

-- Vector index
CREATE VIRTUAL TABLE IF NOT EXISTS session_chunks_vec
USING vec0(
    rowid INTEGER PRIMARY KEY,
    embedding FLOAT[384]
);
```

### knowledge_chunks (new)

```sql
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,           -- file path or "api:<doc_id>"
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    hit_count INTEGER NOT NULL DEFAULT 0,
    last_accessed TEXT
);

CREATE INDEX IF NOT EXISTS idx_knowledge_source
ON knowledge_chunks(source_path);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_vec
USING vec0(
    rowid INTEGER PRIMARY KEY,
    embedding FLOAT[384]
);
```

### knowledge_meta (new)

```sql
CREATE TABLE IF NOT EXISTS knowledge_meta (
    source_path TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK(source_type IN ('file', 'api')),
    file_hash TEXT,                     -- SHA256 for change detection
    chunk_count INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

### Embedding Dimension Handling

The vec table dimension (`FLOAT[384]`) is determined at creation time by querying `embedder.dimension`. If the embedder model changes (producing a different dimension), the vec table must be dropped and recreated, with a full reindex from the parent table's BLOB embeddings. The parent table stores `embedding BLOB` without a fixed dimension, so dimension changes don't require re-embedding the source text — only the vec index is rebuilt.

### Key Design Decisions

- **session_chunks vs knowledge_chunks are separate tables** because their lifecycles differ: L2 chunks are evicted with sessions, L3 chunks are evicted with document removal.
- **sqite-vec virtual tables** reference rowid from the parent table, keeping the BLOB embedding in the parent for fallback (when sqlite-vec is unavailable, brute-force cosine still works).
- **hit_count and last_accessed** are added to both chunk tables for recency decay and frequency scoring.
- **knowledge_meta** tracks file hashes for incremental sync on startup.

### Migration (PRAGMA user_version 1 → 2)

```python
def _migrate_to_memory_v2(conn):
    # Add columns (idempotent via try/except)
    for col, defn in [
        ("hit_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_accessed", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE session_chunks ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass

    # Create vec tables
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS session_chunks_vec
        USING vec0(rowid INTEGER PRIMARY KEY, embedding FLOAT[384])
    """)

    # Backfill existing embeddings into vec table
    count = conn.execute("SELECT COUNT(*) FROM session_chunks_vec").fetchone()[0]
    if count == 0:
        conn.execute("""
            INSERT INTO session_chunks_vec (rowid, embedding)
            SELECT id, embedding FROM session_chunks
        """)

    conn.execute("PRAGMA user_version = 2")
```

## Core Components

### ShortTermMemory (L1)

```
manage(messages, round_num) → (mutated_messages, summary_text)
│
├─ round_num ≤ max_full_rounds
│     → return messages unchanged
│
├─ round_num ≤ max_stripped_rounds
│     → keep last max_full_rounds full; strip older rounds
│       (tool_result reduced to "tool_name: file_path +/-N")
│
├─ max_stripped_rounds < round_num ≤ max_summary_rounds
│     → try LLM summarization of rounds beyond the window; on success
│       return (stripped_messages, summary_text) for the caller to inject
│
└─ round_num > max_summary_rounds
      → strip only, no summary (conversation too long to spend tokens every round)
```

**Strip** (`_strip_old_rounds`): Reduces tool_result content from full diff/output to a one-line summary like `"edit_file: README.md +3/-1"`.

**Summarize** (`_summarize_old_rounds`): Calls the LLM with a summarization prompt. Old rounds beyond the window are stripped to one-liners as usual; the summary is returned as `summary_text` for the caller to inject into the next prompt. A sync `manage()` call (no provider) never summarizes — it strips only.

### LongTermMemory (L2)

Refactored from the existing `SessionMemory`. Same store path (chunk by file, batch embed, write), but retrieval changes:

**Retrieval**:
1. Generate query embedding via `embedder.embed(query)`.
2. `session_chunks_vec` MATCH with `coarse_recall_limit` → candidate rowids + distances.
3. JOIN `session_chunks` to get `hit_count`, `last_accessed`, `payload_json`.
4. Python fine-ranking:
   ```
   final_score = cosine_similarity
               × exp(-recency_decay_rate × days_since_last_access)
               + hit_count_weight × min(hit_count, 10)
   ```
   - When `last_accessed` is NULL or unparseable, `days_since_last_access` is unavailable and decay defaults to 1.0 (no penalty).
   - `min(hit_count, 10)` caps frequency bonus to prevent a few hot chunks from dominating.
5. Filter by `similarity_threshold`.
6. Group by session, pick top-2 per session (prefer `file_diff` chunks).
7. Sort across sessions, return top `max_entries`.
8. After retrieval: `UPDATE session_chunks SET hit_count = hit_count + 1, last_accessed = datetime('now') WHERE id IN (...)`.

**Fallback**: If sqlite-vec is unavailable (extension not loaded), fall back to the current brute-force `query_chunks()` path.

### KnowledgeBase (L3)

**File Sync** (`sync_files()`):
1. Scan `knowledge_dir` for `.md` and `.txt` files.
2. Compute SHA256 per file.
3. Diff against `knowledge_meta`: add, update (re-chunk), remove.
4. For each new/updated file: split into overlapping chunks → batch embed → insert into `knowledge_chunks` + `knowledge_chunks_vec` → upsert `knowledge_meta`.
5. Return `{"added": N, "updated": N, "removed": N}`.

**API Endpoints** (new in `router.py`):
- `POST /live-edit/knowledge` — upload a document snippet. Body: `{source_path, content, metadata}`. `source_path` must use `"api:<name>"` prefix.
- `DELETE /live-edit/knowledge/{source_path}` — remove API-uploaded knowledge only (reject `source_type='file'`).
- `GET /live-edit/knowledge` — list all knowledge entries with metadata.

**Search** (`search(query)`):
1. Same sqlite-vec MATCH → cosine ranking as L2, but queries `knowledge_chunks_vec`.
2. No recency decay or hit_count weighting for knowledge (documents are timeless).
3. Return top `max_entries` as `KnowledgeEntry(source_path, chunk_text, score)`.

**Trigger**: Only invoked when L2 `retrieve()` returns an empty list AND `knowledge.enabled = True`. The condition is "L2 empty", not a separate score threshold — this avoids the logical gap where L2 filters out chunks below `similarity_threshold` but L3's trigger threshold is lower.

### Retrieval Result Format

```
## Relevant Past Changes

1. "update README title" (85%) -> README.md
   feat: update project title

2. "fix import order" (72%) -> models.py
   isort models.py

Use the above as reference only -- do not blindly copy past solutions.

## Project Knowledge

- coding-style.md: "All Python files use 4-space indentation, line width 100..."
- api-reference.md: "POST /api/users — create user, required fields: name, email"
```

L2 and L3 sections are separated by a blank line. The L3 section is omitted when no knowledge results are found.

## Engine Integration

`engine.py` changes:

| Location | Current | New |
|----------|---------|-----|
| Setup (lines 554-578) | Manual `LocalEmbedder()` + `SessionMemory()` construction + startup validation | `MemoryManager(storage, embedder, config.memory)` single construction |
| Continue path (lines 594-603) | `session_memory.retrieve(continue_msg)` | `memory.retrieve(query, session_id, messages, round_num)` |
| New session path (lines 611-621) | `session_memory.retrieve(session.request)` | Same unified `memory.retrieve()` call |
| Store (lines 443-454) | `session_memory.store(...)` | `memory.store(...)` — delegates to L2 only |

`Config` gains a `memory: MemoryConfig` field. `parse_config` populates it from the `[memory]` TOML section, with `[session_memory]` as fallback for `long_term`.

## Error Handling

- **sqlite-vec unavailable**: Log warning, fall back to brute-force cosine retrieval for both L2 and L3.
- **Embedder load failure**: Log warning, disable L2 and L3 for this session (same as current behavior).
- **LLM summarization failure (L1)**: Log warning, fall back to strip-only behavior — old rounds are stripped rather than summarized, trading context preservation for safety.
- **File sync failure (L3)**: Log warning with the specific file path, skip that file, continue with remaining files.
- **API upload failure (L3)**: Return 4xx/5xx to caller, no partial state (writes use transactions).
- **Validation failure**: `__post_init__` raises `ValueError` at config parse time, preventing startup with invalid config.

## Testing

### Unit Tests

| Component | Test File | Key Cases |
|-----------|-----------|-----------|
| ShortTermConfig | `tests/test_memory_config.py` | `__post_init__` validation errors |
| LongTermConfig | same | Validation ranges, backward compat alias |
| KnowledgeConfig | same | `chunk_overlap >= chunk_size` rejection |
| ShortTermMemory | `tests/test_short_term.py` | No-op when under threshold, strip format, summary trigger |
| LongTermMemory | `tests/test_long_term.py` | Cosine scoring, recency decay formula, hit_count cap, session grouping |
| KnowledgeBase | `tests/test_knowledge.py` | Chunk splitting, SHA256 diff, API upload, delete rejection for files |
| MemoryManager | `tests/test_memory_manager.py` | L1+L2 combined output, L2→L3 fallback, empty L2 skips L3 when disabled |

### Integration Tests

| Scenario | Verification |
|----------|-------------|
| Full retrieval flow with all tiers enabled | Context string contains both L2 and L3 sections |
| sqlite-vec unavailable fallback | Retrieval succeeds via brute-force, warning logged |
| Session store + retrieve round-trip | Stored chunks are findable by related query |
| L1 summary triggered after many rounds | Old rounds replaced with summary, recent rounds intact |
| Config backward compat | Old `[session_memory]` TOML still works, values flow to `long_term` |

## Dependencies

- `sqlite-vec` (new): `pip install sqlite-vec>=0.1.0`
- `sentence-transformers` (existing, optional): L2 and L3 require this or an API-based embedder

## Files Changed

| File | Change |
|------|--------|
| `live_edit/config.py` | Add `MemoryConfig`, `ShortTermConfig`, `LongTermConfig` (rename), `KnowledgeConfig` |
| `live_edit/memory.py` | **New** — `MemoryManager`, `ShortTermMemory`, `LongTermMemory`, `KnowledgeBase` |
| `live_edit/storage.py` | Migration to v2, new methods for knowledge CRUD, vec table creation |
| `live_edit/session_memory.py` | Deprecate — re-export from `memory.py` for backward compat |
| `live_edit/router.py` | Add `POST/DELETE/GET /live-edit/knowledge` endpoints |
| `live_edit/engine.py` | Replace `SessionMemory` usage with `MemoryManager` |
| `pyproject.toml` | Add `sqlite-vec` to optional `[rag]` dependencies |
| `docs/onboarding.md` | Update `[session_memory]` docs to `[memory]` |
