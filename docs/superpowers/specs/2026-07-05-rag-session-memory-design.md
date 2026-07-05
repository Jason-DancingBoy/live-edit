# RAG Session Memory — Design Spec

**Date:** 2026-07-05
**Status:** approved
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
    |-- retrieve(): search on new session
    |-- scoring: semantic(0.7) + file_overlap(0.3)
    |
    v
Storage (SQLite)                       <-- MODIFY: new table session_embeddings
    |
    v
Engine (agent loop)                    <-- MODIFY: inject history into system prompt
```

### 2.2 Embedder Interface (`embedder.py`)

```python
class Embedder(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]: ...
    
    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
    
    @property
    @abstractmethod
    def dimension(self) -> int: ...
```

Default `LocalEmbedder`:
- Model: `all-MiniLM-L6-v2` (384-dim, ~80MB, sentence-transformers)
- Lazy loading on first call
- `embed_batch()` for efficiency

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
    
    async def store(self, session_id: str, request: str, files: list[str]) -> None: ...
    
    async def retrieve(self, request: str, top_k: int, threshold: float) -> list[MemoryEntry]: ...
```

Scoring:
```python
semantic_sim = cosine_similarity(request_embedding, memory_entry.embedding)
file_sim = jaccard(current_files, memory_entry.files)  # or 0.0 if no current files
score = semantic_sim * 0.7 + file_sim * 0.3
```

### 2.4 Storage Schema

New table in SQLite:

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

### 2.5 Configuration

```toml
[session_memory]
enabled = true
max_entries = 10
similarity_threshold = 0.6
semantic_weight = 0.7
file_overlap_weight = 0.3

[session_memory.embedder]
type = "local"           # "local" | "api" | custom plugin name
model = "all-MiniLM-L6-v2"
api_url = ""
api_key_env = ""
```

### 2.6 Engine Integration

**Write path** — after commit succeeds:
```python
if config.session_memory.enabled:
    await session_memory.store(session_id, request, session.modified_files)
```

**Read path** — during system prompt construction:
```python
if config.session_memory.enabled:
    memories = await session_memory.retrieve(request, top_k, threshold)
    if memories:
        system_prompt += "\n\n" + _format_memory_context(memories)
```

**Injected format:**
```
## 历史相似编辑记录

以下是你过去处理过的相似请求，可以作为参考：

1. 请求: "重构 auth.py 的登录逻辑"
   修改文件: auth.py, session.py
   Commit: abc1234
   相似度: 0.89

请参考以上历史经验，但不要盲目照搬——每次请求可能有不同的具体需求。
```

## 3. Edge Cases

| Scenario | Handling |
|---|---|
| No historical sessions | `retrieve()` returns empty list, no injection |
| >5000 stored sessions | Brute-force cosine similarity acceptable up to ~5000 rows; add approximate index later if needed |
| Model load failure | Catch exception, set `enabled = false`, log warning, proceed normally |
| Dimension mismatch | Validate on `store()`, raise error |
| Session reverted | Keep embedding record (reverted sessions still carry useful experience) |
| Duplicate requests | Don't deduplicate; multiple similar records provide iteration context |
| Storage write failure | Catch, log warning, don't block commit |

## 4. Dependencies

```toml
[project.optional-dependencies]
rag = ["sentence-transformers>=3.0"]
```

If `session_memory.enabled = true` but the dependency is missing, raise a clear `ImportError` at startup.

## 5. Testing

### Unit: `tests/test_embedder.py`
- Mock `sentence-transformers`, verify `embed()` and `embed_batch()` return correct dimensions
- Lazy loading: first call triggers model load, subsequent calls don't

### Unit: `tests/test_session_memory.py`
- Fake embedder (fixed vectors), test `store()` → `retrieve()` round-trip
- Empty history returns `[]`
- Identical requests score near 1.0
- Threshold filters out low-similarity entries
- top_k truncation
- File overlap bonus

### Integration: `tests/test_session_memory_integration.py`
- Real SQLite, full store → retrieve flow
- Multiple entries, verify top-k ordering
- Engine test: verify system prompt contains history context when enabled

### Existing tests
- Engine tests: add `session_memory` field to mock Config (default disabled), no existing assertions break

## 6. Files Changed

| File | Action |
|---|---|
| `live_edit/embedder.py` | NEW — Embedder interface + LocalEmbedder |
| `live_edit/session_memory.py` | NEW — SessionMemory class |
| `live_edit/config.py` | MODIFY — add SessionMemoryConfig + EmbedderConfig |
| `live_edit/storage.py` | MODIFY — add `session_embeddings` table + read/write methods |
| `live_edit/engine.py` | MODIFY — store on commit, retrieve + inject on session start |
| `pyproject.toml` | MODIFY — optional-dependency `rag` |
