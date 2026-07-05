# RAG Session Memory — Design Spec

**Date:** 2026-07-05
**Status:** approved (revised after review)
**Scope:** live-edit agent framework

## 1. Motivation

live-edit currently treats every edit session as a blank slate. When a user requests something similar to a past edit ("refactor auth.py" after someone previously "cleaned up auth module"), the agent has no memory of the earlier session. Adding retrieval-augmented session memory lets the agent automatically reference relevant historical sessions, improving consistency and reducing repeated mistakes.

## 2. Design

### 2.1 Architecture

Two new modules, following the existing pluggable pattern:

```
Config (.live-edit.toml)
    |
    v
Embedder (abstract interface)          <-- NEW: embedder.py
    |-- LocalEmbedder (default)
    |-- (user-implemented custom)
    |
SessionMemory (retrieval logic)        <-- NEW: session_memory.py
    |-- store(): write on commit
    |-- retrieve(): search on new session / continuation
    |-- scoring: cosine similarity (semantic only for v1)
    |
    v
Storage (abstract interface)           <-- MODIFY: new abstract methods
    |-- store_embedding()
    |-- query_embeddings()
    |-- delete_old_embeddings()
    |
    v
Engine (agent loop)                    <-- MODIFY: inject history into system prompt
                                            (both new sessions AND continuations)
```

### 2.2 Embedder Interface (`embedder.py`)

```python
class Embedder(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]: ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Default: loop embed(). Override for optimized batch inference."""
        return [self.embed(t) for t in texts]

    @property
    @abstractmethod
    def dimension(self) -> int: ...
```

Key design rules:
- **All `embed()` and `embed_batch()` calls MUST run via `loop.run_in_executor()`** at the call site (in `SessionMemory`). Embedder implementations are synchronous and CPU-bound; the caller is responsible for not blocking the async event loop.
- `embed_batch()` has a default implementation in the ABC — custom embedders override it only if they have native batch support.

Default `LocalEmbedder`:
- Model: `all-MiniLM-L6-v2` (384-dim, ~80MB download, sentence-transformers)
- Lazy loading on first call, protected by `threading.Lock` to prevent race conditions on concurrent first calls
- Thread-safe after initialization
- **Startup validation**: if `session_memory.enabled = true`, the engine calls `embedder.embed("test")` once at startup (non-blocking via `run_in_executor`). Failure disables session memory with a clear warning — no mid-session surprises.

### 2.3 SessionMemory (`session_memory.py`)

```python
@dataclass
class MemoryEntry:
    session_id: str
    request: str
    files: set[str]
    commit_hash: str
    score: float

class SessionMemory:
    def __init__(self, storage: Storage, embedder: Embedder, config: SessionMemoryConfig): ...

    async def store(self, session_id: str, request: str, files: list[str]) -> None:
        """Compute embedding via run_in_executor, then call storage.store_embedding()."""

    async def retrieve(self, request: str) -> list[MemoryEntry]:
        """Compute embedding via run_in_executor, call storage.query_embeddings(),
        compute cosine similarity for each row, filter by threshold,
        sort desc, truncate to top_k, return results.
        top_k and threshold read from self.config."""

    async def _evict_if_needed(self) -> None:
        """If row count > config.max_stored_entries, delete oldest entries
        via storage.delete_old_embeddings()."""
```

**Scoring (v1 — semantic only):**

File overlap scoring is deferred to a future version because at retrieval time (session start), `session.modified_files` is empty and there is no reliable way to extract file references from the user's natural language request. v1 uses pure cosine similarity:

```python
score = cosine_similarity(request_embedding, row_embedding)
```

**Retrieval algorithm (explicit):**

1. Get request embedding via `run_in_executor(embedder.embed, request)`
2. Call `storage.query_embeddings()` — returns all rows (session_id, request, files_json, embedding_blob)
3. Deserialize each BLOB via `struct.unpack(f'{dim}f', blob)`
4. Compute cosine similarity for each row
5. Filter: drop rows where score < `similarity_threshold`
6. Sort: descending by score
7. Truncate: keep top `max_entries` results
8. Return `list[MemoryEntry]`

### 2.4 Storage Interface Changes

Two new abstract methods on `Storage`:

```python
class Storage(ABC):
    # ... existing methods ...

    @abstractmethod
    async def store_embedding(self, session_id: str, request: str,
                              files_json: str, embedding: bytes) -> None: ...

    @abstractmethod
    async def query_embeddings(self) -> list[tuple[str, str, str, bytes]]:
        """Returns list of (session_id, request, files_json, embedding_blob)."""

    @abstractmethod
    async def delete_old_embeddings(self, keep_count: int) -> None: ...
```

SQLiteStorage implementation:

```sql
CREATE TABLE IF NOT EXISTS session_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    request TEXT NOT NULL,
    files_json TEXT NOT NULL,
    embedding BLOB NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

- `store_embedding()`: `INSERT OR REPLACE INTO session_embeddings ...`
- `query_embeddings()`: `SELECT session_id, request, files_json, embedding FROM session_embeddings ORDER BY created_at DESC`
- `delete_old_embeddings()`: `DELETE FROM session_embeddings WHERE id NOT IN (SELECT id FROM session_embeddings ORDER BY created_at DESC LIMIT ?)`
- `files_json`: `json.dumps(files, ensure_ascii=False)` for non-ASCII path support
- `embedding`: `struct.pack(f'{dim}f', *vector)` — float32 little-endian, language-agnostic, ~4 bytes per dimension
- Deserialization: `struct.unpack(f'{dim}f', blob)` returns tuple of floats
- `_init_db()` creates the table via `CREATE TABLE IF NOT EXISTS` — backward-compatible with existing databases

### 2.5 Configuration

```toml
[session_memory]
enabled = true
max_entries = 10               # top-K results returned per retrieval
similarity_threshold = 0.6     # minimum cosine similarity
max_stored_entries = 5000      # eviction threshold: delete oldest when exceeded
memory_prompt_template = ""    # optional custom template for injected context

[session_memory.embedder]
type = "local"                 # "local" | "api" | custom plugin name
model = "all-MiniLM-L6-v2"
api_url = ""                   # only for type="api"
api_key_env = ""               # only for type="api"
```

Config dataclasses:

```python
@dataclass
class EmbedderConfig:
    type: str = "local"
    model: str = "all-MiniLM-L6-v2"
    api_url: str = ""
    api_key_env: str = ""

@dataclass
class SessionMemoryConfig:
    enabled: bool = False
    max_entries: int = 10
    similarity_threshold: float = 0.6
    max_stored_entries: int = 5000
    memory_prompt_template: str = ""
    embedder: EmbedderConfig = field(default_factory=EmbedderConfig)
```

### 2.6 Engine Integration

**Write path** — inside `_do_commit()`, after `vcs.commit_in_worktree()` succeeds but before `vcs.remove_worktree_dir()`:

```python
if config.session_memory.enabled:
    await session_memory.store(
        session_id=session.id,
        request=request,
        files=session._modified_files  # set before _do_commit is called
    )
```

**Read path (new session)** — in `run_edit_session()`, after system prompt construction:

```python
if config.session_memory.enabled:
    memories = await session_memory.retrieve(request)
    if memories:
        memory_context = _format_memory_context(memories, config)
        messages[0]["content"] += "\n\n" + memory_context
```

**Read path (continuation)** — in `continue_edit_session()`, before resuming the agent loop:

```python
if config.session_memory.enabled:
    memories = await session_memory.retrieve(request)
    if memories:
        memory_context = _format_memory_context(memories, config)
        messages.append({"role": "system", "content": memory_context})
```

Continuation sessions append memory as a new system message rather than modifying the first message, preserving the original conversation context.

**Injected format (default template):**

```
## Historical Similar Edit Records

The following are past requests similar to the current one. Reference them
for patterns and solutions, but adapt to the specific current request.

1. Request: "refactor auth.py login logic"
   Files modified: auth.py, session.py
   Commit: abc1234
   Similarity: 0.89

2. Request: "fix login page styling"
   Files modified: auth.py, login.css
   Commit: def5678
   Similarity: 0.72

Use the above as reference only — do not blindly copy past solutions.
```

The template can be overridden via `memory_prompt_template` in config. The default is English; users can set a Chinese template if desired.

## 3. Edge Cases

| Scenario | Handling |
|---|---|
| No historical sessions | `retrieve()` returns empty list, no injection |
| First call, model not loaded | Lazy loading behind `threading.Lock`, startup validation catches failures early |
| Model download/load fails at startup | Catch exception, set `enabled = false`, log warning, proceed normally |
| Embedding call fails mid-session | Catch exception, log warning, skip memory for this session |
| Stored entries exceed `max_stored_entries` | Evict oldest entries after each `store()` call |
| Session reverted | Keep embedding record (reverted sessions still carry useful experience) |
| Duplicate requests | Don't deduplicate; multiple similar records provide iteration context |
| Storage write failure | Catch, log warning, don't block commit |
| Concurrent session commits | SQLite WAL handles concurrent reads; `INSERT OR REPLACE` is atomic per-write. Writes serialize naturally via SQLite's write lock. |
| Non-ASCII file paths | `json.dumps(files, ensure_ascii=False)` preserves original characters |
| Missing `[rag]` dependency | Raise clear `ImportError` at startup: "session_memory.enabled=true requires sentence-transformers. Install with: pip install live-edit[rag]" |
| Existing database (no session_embeddings table) | `CREATE TABLE IF NOT EXISTS` — backward compatible, existing `live_edit_sessions` table untouched |
| Empty `modified_files` on store | Store with `files_json = "[]"` — still indexed by request semantics |
| `session_id` collision (continuation recommit) | `INSERT OR REPLACE` updates the existing record with latest state |

## 4. Dependencies

```toml
[project.optional-dependencies]
rag = ["sentence-transformers>=3.0"]
```

**Dependency footprint:** `sentence-transformers` pulls in `torch` (~800MB), `transformers` (~500MB), `numpy`, and `tokenizers`. Total install size is ~2-3 GB. The `[rag]` optional label keeps the default `pip install live-edit` lightweight. Users who enable session memory must accept this footprint or provide their own API-based embedder.

## 5. Performance Notes

- Embedding calls run via `run_in_executor` to avoid blocking the async event loop
- Retrieval: reads all embedding BLOBs from SQLite (384 dim × 4 bytes × N rows). At 5000 rows = ~7.5 MB read, ~50-200ms via `run_in_executor`. Acceptable for session startup.
- Store: one `INSERT OR REPLACE` + eviction check. Negligible overhead.
- Startup validation: one `embed("test")` call, ~100-500ms (including model load on first run), non-blocking
- Caching: no in-memory cache in v1. If retrieval becomes a bottleneck, add a TTL cache of all embeddings in `SessionMemory`.

## 6. Testing

### Unit: `tests/test_embedder.py`
- Mock `sentence-transformers`, verify `embed()` returns correct dimension
- `embed_batch()` default implementation works via loop
- Lazy loading protected by `threading.Lock`
- Startup validation: test failure disables memory cleanly

### Unit: `tests/test_session_memory.py`
- Fake embedder (fixed vectors), fake storage (in-memory list), test `store()` → `retrieve()` round-trip
- Empty history returns `[]`
- Identical requests score near 1.0
- Threshold filters out low-similarity entries
- top_k truncation
- Eviction: inserting > max_stored_entries triggers cleanup
- `retrieve()` runs embedder via `run_in_executor` (verify with mock executor)

### Unit: `tests/test_storage.py`
- `store_embedding()` → `query_embeddings()` round-trip
- `delete_old_embeddings()` keeps correct count
- Non-ASCII file paths round-trip correctly via `ensure_ascii=False`
- BLOB serialization: `struct.pack`/`struct.unpack` round-trip matches original vector
- Existing database without `session_embeddings` table: `_init_db()` creates it, old data untouched

### Integration: `tests/test_session_memory_integration.py`
- Real SQLite, full store → retrieve flow
- Multiple entries, verify top-k ordering
- Concurrent writes from separate threads don't corrupt

### Integration: `tests/test_engine.py`
- Engine with session_memory enabled + fake embedder: system prompt contains injected history
- Engine with session_memory disabled: no injection, no behavioral change
- Continuation session: receives memory injection as separate system message
- Missing `[rag]` dependency: raises clear ImportError with install instructions
- All existing engine tests pass unchanged (SessionMemoryConfig defaults to `enabled=false`)

## 7. Files Changed

| File | Action |
|---|---|
| `live_edit/embedder.py` | NEW — Embedder ABC + LocalEmbedder |
| `live_edit/session_memory.py` | NEW — SessionMemory class |
| `live_edit/config.py` | MODIFY — add SessionMemoryConfig + EmbedderConfig dataclasses |
| `live_edit/storage.py` | MODIFY — add abstract methods to Storage ABC; implement in SQLiteStorage |
| `live_edit/engine.py` | MODIFY — store on commit, retrieve + inject on new session and continuation |
| `pyproject.toml` | MODIFY — optional-dependency `[rag]` |
