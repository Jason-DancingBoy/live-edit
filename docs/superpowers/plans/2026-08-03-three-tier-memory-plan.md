# Three-Tier Memory System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat `SessionMemory` with a three-tier memory architecture (L1 short-term window, L2 long-term sqlite-vec + recency decay, L3 knowledge base RAG) unified under `MemoryManager`.

**Architecture:** New `live_edit/memory.py` holds `MemoryManager`, `ShortTermMemory`, `LongTermMemory`, and `KnowledgeBase`. Config expanded with `MemoryConfig` and sub-configs. Storage gains migration v2, knowledge tables, and sqlite-vec virtual tables. Router gains knowledge API endpoints. Engine wires `MemoryManager` in place of `SessionMemory`.

**Tech Stack:** Python 3.10+, dataclasses, sqlite-vec>=0.1.0, sentence-transformers (existing optional dep), FastAPI, pytest + pytest-asyncio

## Global Constraints

- Python >=3.10, tomli for TOML parsing on <3.11
- sqlite-vec added to `[project.optional-dependencies]` `rag` extra
- `SessionMemoryConfig = LongTermConfig` alias must exist at module level in `config.py`
- `Config.session_memory` property must delegate to `self.memory.long_term`
- `[session_memory]` TOML section continues to work, `[memory.long_term]` takes priority
- `memory.enabled` and `long_term.enabled` default `False` (opt-in, preserve existing behavior)
- All `__post_init__` validators raise `ValueError` on invalid config
- sqlite-vec unavailable → log warning → fall back to brute-force cosine
- Embedder load failure → log warning → disable L2/L3 for session
- File sync failures (L3) → log warning + skip file, continue
- API upload failures → return HTTP error, transaction rollback
- All new public methods need type annotations

---

### Task 1: Config Dataclasses

**Files:**
- Modify: `live_edit/config.py`
- Test: `tests/test_memory_config.py` (create)

**Interfaces:**
- Produces: `ShortTermConfig`, `LongTermConfig`, `KnowledgeConfig`, `MemoryConfig` dataclasses
- Produces: `SessionMemoryConfig = LongTermConfig` alias
- Produces: `Config.memory: MemoryConfig` field, `Config.session_memory` property
- Produces: `parse_config` extended to parse `[memory]` TOML section with `[session_memory]` fallback

- [ ] **Step 1: Write tests for config dataclasses**

```python
# tests/test_memory_config.py
import pytest
from live_edit.config import (
    ShortTermConfig,
    LongTermConfig,
    KnowledgeConfig,
    MemoryConfig,
    SessionMemoryConfig,
    EmbedderConfig,
)


class TestShortTermConfig:
    def test_defaults(self):
        cfg = ShortTermConfig()
        assert cfg.enabled is True
        assert cfg.max_full_rounds == 3
        assert cfg.max_stripped_rounds == 7
        assert cfg.max_summary_rounds == 20
        assert cfg.summary_model == ""

    def test_raises_when_stripped_lt_full(self):
        with pytest.raises(ValueError, match="max_stripped_rounds"):
            ShortTermConfig(max_full_rounds=5, max_stripped_rounds=3)

    def test_raises_when_summary_lt_stripped(self):
        with pytest.raises(ValueError, match="max_summary_rounds"):
            ShortTermConfig(max_full_rounds=3, max_stripped_rounds=5, max_summary_rounds=4)

    def test_equal_values_ok(self):
        cfg = ShortTermConfig(max_full_rounds=3, max_stripped_rounds=3, max_summary_rounds=3)
        assert cfg.max_full_rounds == 3


class TestLongTermConfig:
    def test_defaults(self):
        cfg = LongTermConfig()
        assert cfg.enabled is False
        assert cfg.max_entries == 10
        assert cfg.similarity_threshold == 0.6
        assert cfg.max_stored_entries == 5000
        assert cfg.recency_decay_rate == 0.01
        assert cfg.hit_count_weight == 0.05
        assert cfg.coarse_recall_limit == 200

    def test_default_embedder(self):
        cfg = LongTermConfig()
        assert cfg.embedder.type == "local"
        assert cfg.embedder.model == "thenlper/gte-small"

    @pytest.mark.parametrize("field,value", [
        ("similarity_threshold", -0.1),
        ("similarity_threshold", 1.1),
        ("recency_decay_rate", -0.1),
        ("recency_decay_rate", 1.1),
        ("hit_count_weight", -0.1),
        ("hit_count_weight", 1.1),
    ])
    def test_raises_on_out_of_range_float(self, field, value):
        with pytest.raises(ValueError, match=field):
            LongTermConfig(**{field: value})

    def test_raises_on_non_positive_int(self):
        with pytest.raises(ValueError, match="coarse_recall_limit"):
            LongTermConfig(coarse_recall_limit=0)
        with pytest.raises(ValueError, match="max_stored_entries"):
            LongTermConfig(max_stored_entries=0)


class TestKnowledgeConfig:
    def test_defaults(self):
        cfg = KnowledgeConfig()
        assert cfg.enabled is False
        assert cfg.api_enabled is False
        assert cfg.knowledge_dir == ".live-edit/knowledge"
        assert cfg.chunk_size == 500
        assert cfg.chunk_overlap == 50
        assert cfg.max_entries == 20

    def test_raises_when_overlap_gte_chunk_size(self):
        with pytest.raises(ValueError, match="chunk_overlap"):
            KnowledgeConfig(chunk_size=100, chunk_overlap=100)
        with pytest.raises(ValueError, match="chunk_overlap"):
            KnowledgeConfig(chunk_size=100, chunk_overlap=150)


class TestMemoryConfig:
    def test_defaults(self):
        cfg = MemoryConfig()
        assert cfg.enabled is False
        assert isinstance(cfg.short_term, ShortTermConfig)
        assert isinstance(cfg.long_term, LongTermConfig)
        assert isinstance(cfg.knowledge, KnowledgeConfig)

    def test_nested_default_factories_isolated(self):
        m1 = MemoryConfig()
        m2 = MemoryConfig()
        m1.short_term.max_full_rounds = 99
        assert m2.short_term.max_full_rounds == 3


class TestSessionMemoryConfigAlias:
    def test_alias_is_long_term_config(self):
        assert SessionMemoryConfig is LongTermConfig

    def test_alias_constructs(self):
        cfg = SessionMemoryConfig(enabled=True, max_entries=5)
        assert cfg.enabled is True
        assert cfg.max_entries == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_memory_config.py -v`
Expected: all fail — `ShortTermConfig` etc. not defined

- [ ] **Step 3: Add config dataclasses to config.py**

After `SessionMemoryConfig` (line ~125), add:

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
            raise ValueError(
                f"max_stripped_rounds ({self.max_stripped_rounds}) "
                f"must be >= max_full_rounds ({self.max_full_rounds})"
            )
        if self.max_summary_rounds < self.max_stripped_rounds:
            raise ValueError(
                f"max_summary_rounds ({self.max_summary_rounds}) "
                f"must be >= max_stripped_rounds ({self.max_stripped_rounds})"
            )


@dataclass
class LongTermConfig:
    enabled: bool = False
    max_entries: int = 10
    similarity_threshold: float = 0.6
    max_stored_entries: int = 5000
    recency_decay_rate: float = 0.01
    hit_count_weight: float = 0.05
    coarse_recall_limit: int = 200
    memory_prompt_template: str = ""
    embedder: EmbedderConfig = field(default_factory=EmbedderConfig)

    def __post_init__(self):
        if not (0 <= self.similarity_threshold <= 1):
            raise ValueError(
                f"similarity_threshold must be in [0, 1], got {self.similarity_threshold}"
            )
        if not (0 <= self.recency_decay_rate <= 1):
            raise ValueError(
                f"recency_decay_rate must be in [0, 1], got {self.recency_decay_rate}"
            )
        if not (0 <= self.hit_count_weight <= 1):
            raise ValueError(
                f"hit_count_weight must be in [0, 1], got {self.hit_count_weight}"
            )
        if self.coarse_recall_limit < 1:
            raise ValueError(
                f"coarse_recall_limit must be >= 1, got {self.coarse_recall_limit}"
            )
        if self.max_stored_entries < 1:
            raise ValueError(
                f"max_stored_entries must be >= 1, got {self.max_stored_entries}"
            )


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
            raise ValueError(
                f"chunk_overlap ({self.chunk_overlap}) "
                f"must be < chunk_size ({self.chunk_size})"
            )


@dataclass
class MemoryConfig:
    enabled: bool = False
    short_term: ShortTermConfig = field(default_factory=ShortTermConfig)
    long_term: LongTermConfig = field(default_factory=LongTermConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)


# Backward-compatible alias
SessionMemoryConfig = LongTermConfig  # deprecated — use LongTermConfig directly
```

- [ ] **Step 4: Replace session_memory field with memory field, keep session_memory property**

First, in the `Config` dataclass, **delete** the existing `session_memory: SessionMemoryConfig = field(default_factory=SessionMemoryConfig)` field (original ~line 161). In its place, add the new field:

```python
memory: MemoryConfig = field(default_factory=MemoryConfig)
```

Then add a property to `Config` for backward compat (right after the dataclass body, before `# ── TOML parsing ──`). Keep the property **named** `session_memory` but have it delegate to `self.memory.long_term`:

```python
@property
def session_memory(self) -> LongTermConfig:
    """Backward-compatible accessor for memory.long_term."""
    return self.memory.long_term

@session_memory.setter
def session_memory(self, value: LongTermConfig) -> None:
    self.memory.long_term = value
```

Note: the dataclass **field** named `session_memory` must be removed — keeping both a field and a same-named property conflicts (dataclass processing overwrites the class attribute, and the field ordering would set `self.session_memory` before `self.memory` exists, raising `AttributeError` on every `Config()`). The property is the only `session_memory` accessor; it reads/writes `self.memory.long_term`.

- [ ] **Step 5: Extend parse_config to handle [memory] TOML section**

In `parse_config()`, after the existing `[session_memory]` parsing block (~line 279-294), replace/extend so both `[memory.long_term]` and `[session_memory]` are accepted:

```python
# Parse [memory] section (new) with [session_memory] fallback
mem_data = raw.get("memory", {})

# Short-term
st_data = mem_data.get("short_term", {})
short_term = ShortTermConfig(
    enabled=st_data.get("enabled", True),
    max_full_rounds=st_data.get("max_full_rounds", 3),
    max_stripped_rounds=st_data.get("max_stripped_rounds", 7),
    max_summary_rounds=st_data.get("max_summary_rounds", 20),
    summary_model=st_data.get("summary_model", ""),
)

# Long-term: prefer [memory.long_term], fallback to [session_memory]
lt_data = mem_data.get("long_term", {})
has_memory_section = "long_term" in mem_data

# [session_memory] as fallback
sm_data = raw.get("session_memory", {})
sm_embedder_data = sm_data.get("embedder", {})

# Embedder: [memory.long_term.embedder] > [session_memory.embedder] > default
if "embedder" in lt_data:
    lt_embedder_data = lt_data["embedder"]
    lt_embedder = EmbedderConfig(
        type=lt_embedder_data.get("type", "local"),
        model=lt_embedder_data.get("model", "thenlper/gte-small"),
        api_url=lt_embedder_data.get("api_url", ""),
        api_key_env=lt_embedder_data.get("api_key_env", ""),
    )
elif sm_embedder_data:
    lt_embedder = EmbedderConfig(
        type=sm_embedder_data.get("type", "local"),
        model=sm_embedder_data.get("model", "thenlper/gte-small"),
        api_url=sm_embedder_data.get("api_url", ""),
        api_key_env=sm_embedder_data.get("api_key_env", ""),
    )
else:
    lt_embedder = EmbedderConfig()

long_term = LongTermConfig(
    enabled=(
        lt_data.get("enabled", False)
        if has_memory_section
        else sm_data.get("enabled", False)
    ),
    max_entries=(
        lt_data.get("max_entries", 10)
        if has_memory_section
        else sm_data.get("max_entries", 10)
    ),
    similarity_threshold=(
        lt_data.get("similarity_threshold", 0.6)
        if has_memory_section
        else sm_data.get("similarity_threshold", 0.6)
    ),
    max_stored_entries=(
        lt_data.get("max_stored_entries", 5000)
        if has_memory_section
        else sm_data.get("max_stored_entries", 5000)
    ),
    recency_decay_rate=lt_data.get("recency_decay_rate", 0.01),
    hit_count_weight=lt_data.get("hit_count_weight", 0.05),
    coarse_recall_limit=lt_data.get("coarse_recall_limit", 200),
    memory_prompt_template=(
        lt_data.get("memory_prompt_template", "")
        if has_memory_section
        else sm_data.get("memory_prompt_template", "")
    ),
    embedder=lt_embedder,
)

# Knowledge
kn_data = mem_data.get("knowledge", {})
knowledge = KnowledgeConfig(
    enabled=kn_data.get("enabled", False),
    api_enabled=kn_data.get("api_enabled", False),
    knowledge_dir=kn_data.get("knowledge_dir", ".live-edit/knowledge"),
    chunk_size=kn_data.get("chunk_size", 500),
    chunk_overlap=kn_data.get("chunk_overlap", 50),
    max_entries=kn_data.get("max_entries", 20),
)

memory = MemoryConfig(
    enabled=mem_data.get("enabled", False),
    short_term=short_term,
    long_term=long_term,
    knowledge=knowledge,
)
```

In the `return Config(...)` call, add `memory=memory,`.

- [ ] **Step 6: Update Config return in parse_config**

In the `Config(...)` constructor call at the end of `parse_config()`, add `memory=memory,` and **remove** the old `session_memory=session_memory,` argument (that field no longer exists). The `[session_memory]` parse results now flow through `memory.long_term` (see Step 5: `sm_data` is merged into `long_term` as the fallback when `[memory.long_term]` is absent), so no session_memory argument is passed to `Config`.

- [ ] **Step 7: Update generate_default_config**

In `generate_default_config()`, replace `return Config(...)` to include the new `memory` field with defaults. Do **not** pass `session_memory=SessionMemoryConfig()` — the field was removed in Step 4:

```python
return Config(
    # ... existing fields unchanged ...
    memory=MemoryConfig(),  # new; session_memory now delegated via the property
)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest tests/test_memory_config.py -v`
Expected: all PASS

- [ ] **Step 9: Run existing tests to verify no regressions**

Run: `pytest tests/test_config.py tests/test_session_memory.py tests/test_rag_eval.py -v`
Expected: all PASS (existing imports of `SessionMemoryConfig` still work)

- [ ] **Step 10: Commit**

```bash
git add live_edit/config.py tests/test_memory_config.py
git commit -m "feat: add MemoryConfig dataclasses for three-tier memory system"
```

---

### Task 2: Storage Schema Migration v2 + Knowledge CRUD

**Files:**
- Modify: `live_edit/storage.py`
- Test: `tests/test_storage.py` (extend)

**Interfaces:**
- Consumes: `LongTermConfig` (for embedder dimension)
- Produces: `_migrate_to_memory_v2(conn, embedder_dim)` — idempotent migration
- Produces: `store_knowledge_chunks(source_path, chunks: list[dict])` — transactional upsert
- Produces: `delete_knowledge_chunks(source_path)` — delete all chunks for a document
- Produces: `query_knowledge_chunks(limit)` / `query_knowledge_chunks_vec(query_embedding_bytes, limit)` — retrieval
- Produces: `upsert_knowledge_meta(source_path, source_type, file_hash, chunk_count)` / `delete_knowledge_meta(source_path)` / `list_knowledge_meta()` — metadata CRUD
- Produces: `update_chunk_hit_counts(chunk_ids: list[int])` — increment hit_count + set last_accessed
- Produces: `query_chunks_vec(query_embedding_bytes, limit, dimension)` — sqlite-vec coarse search

- [ ] **Step 1: Write tests for storage migration and knowledge CRUD**

Note: `tests/test_storage.py` already has `import pytest` at the top (confirmed). `test_backfills_vec_table` below uses `pytest.importorskip("sqlite_vec")` so it is skipped when sqlite-vec is not installed instead of erroring on the `session_chunks_vec` count.

```python
# Add to tests/test_storage.py

class TestMigrationV2:
    def test_adds_hit_columns_to_session_chunks(self, tmp_path):
        from live_edit.storage import SQLiteStorage
        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))
        conn = store._get_conn()

        # Verify columns exist after migration
        cols = [r[1] for r in conn.execute("PRAGMA table_info(session_chunks)")]
        assert "hit_count" in cols
        assert "last_accessed" in cols

    def test_migration_idempotent(self, tmp_path):
        from live_edit.storage import SQLiteStorage
        db = tmp_path / "test.db"
        store1 = SQLiteStorage(str(db))
        conn1 = store1._get_conn()
        version1 = conn1.execute("PRAGMA user_version").fetchone()[0]

        # Re-open and re-migrate
        store2 = SQLiteStorage(str(db))
        conn2 = store2._get_conn()
        version2 = conn2.execute("PRAGMA user_version").fetchone()[0]

        assert version1 == version2

    def test_backfills_vec_table(self, tmp_path):
        # Requires sqlite-vec (installed via the dev/rag extras); skip otherwise.
        pytest.importorskip("sqlite_vec")
        from live_edit.storage import SQLiteStorage
        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))

        # Insert a chunk manually
        conn = store._get_conn()
        import struct
        emb = struct.pack("384f", *[0.1] * 384)
        conn.execute(
            """INSERT INTO session_chunks
               (session_id, commit_hash, chunk_type, chunk_text,
                payload_json, file_path, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("s1", "abc", "request", "test request", "{}", "", emb),
        )
        conn.commit()

        # Re-open to trigger migration (vec backfill)
        store2 = SQLiteStorage(str(db))
        conn2 = store2._get_conn()
        count = conn2.execute("SELECT COUNT(*) FROM session_chunks_vec").fetchone()[0]
        assert count == 1


class TestKnowledgeCRUD:
    def test_store_and_query_knowledge_chunks(self, tmp_path):
        from live_edit.storage import SQLiteStorage
        import struct
        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))
        emb = struct.pack("384f", *[0.1] * 384)

        chunks = [
            {
                "source_path": "api:test",
                "chunk_index": 0,
                "chunk_text": "some knowledge",
                "embedding_bytes": emb,
                "metadata_json": '{"title": "Test"}',
            }
        ]
        store.store_knowledge_chunks("api:test", chunks)
        store.upsert_knowledge_meta("api:test", "api", None, 1)

        rows = store.query_knowledge_chunks(limit=10)
        assert len(rows) == 1
        assert rows[0][1] == "api:test"

    def test_delete_knowledge_chunks(self, tmp_path):
        from live_edit.storage import SQLiteStorage
        import struct
        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))
        emb = struct.pack("384f", *[0.1] * 384)

        store.store_knowledge_chunks("api:test", [
            {"source_path": "api:test", "chunk_index": 0,
             "chunk_text": "x", "embedding_bytes": emb, "metadata_json": "{}"}
        ])
        store.upsert_knowledge_meta("api:test", "api", None, 1)
        store.delete_knowledge_chunks("api:test")
        store.delete_knowledge_meta("api:test")

        rows = store.query_knowledge_chunks(limit=10)
        assert len(rows) == 0

    def test_list_knowledge_meta(self, tmp_path):
        from live_edit.storage import SQLiteStorage
        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))

        store.upsert_knowledge_meta("doc1.md", "file", "abc123", 3)
        store.upsert_knowledge_meta("doc2.md", "file", "def456", 1)

        meta = store.list_knowledge_meta()
        assert len(meta) == 2
        paths = {m["source_path"] for m in meta}
        assert paths == {"doc1.md", "doc2.md"}

    def test_update_chunk_hit_counts(self, tmp_path):
        from live_edit.storage import SQLiteStorage
        import struct
        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))
        emb = struct.pack("384f", *[0.1] * 384)
        conn = store._get_conn()
        conn.execute(
            """INSERT INTO session_chunks
               (session_id, commit_hash, chunk_type, chunk_text,
                payload_json, file_path, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("s1", "abc", "request", "req", "{}", "", emb),
        )
        conn.commit()
        chunk_id = conn.execute("SELECT id FROM session_chunks").fetchone()[0]

        store.update_chunk_hit_counts([chunk_id])

        row = conn.execute(
            "SELECT hit_count, last_accessed FROM session_chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        assert row["hit_count"] == 1
        assert row["last_accessed"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_storage.py::TestMigrationV2 tests/test_storage.py::TestKnowledgeCRUD -v`
Expected: FAIL — methods not defined

- [ ] **Step 3: Create knowledge tables in _init_db**

In `SQLiteStorage._init_db()`, after the `session_chunks` table creation block, add:

```python
# Knowledge base tables (v2)
conn.execute("""
    CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_path TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        chunk_text TEXT NOT NULL,
        embedding BLOB NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now')),
        hit_count INTEGER NOT NULL DEFAULT 0,
        last_accessed TEXT
    )
""")
conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_knowledge_source
    ON knowledge_chunks(source_path)
""")
conn.execute("""
    CREATE TABLE IF NOT EXISTS knowledge_meta (
        source_path TEXT PRIMARY KEY,
        source_type TEXT NOT NULL CHECK(source_type IN ('file', 'api')),
        file_hash TEXT,
        chunk_count INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )
""")
```

- [ ] **Step 4: Load sqlite-vec extension in _get_conn**

The `vec0` virtual tables created in Step 5 only work if the sqlite-vec extension is loaded on every connection. At the top of `live_edit/storage.py`, add:

```python
import logging

logger = logging.getLogger("live-edit.storage")
```

(If a `logger` already exists in the module, keep the existing definition.) In `SQLiteStorage._get_conn`, right after `self._local.conn.execute("PRAGMA journal_mode=WAL")`, add:

```python
# Load sqlite-vec extension (optional; brute-force fallback if unavailable)
try:
    import sqlite_vec
    self._local.conn.enable_load_extension(True)
    sqlite_vec.load(self._local.conn)
except Exception:
    pass
```

Without this load, the `CREATE VIRTUAL TABLE ... USING vec0(...)` statements in `_migrate_to_memory_v2` raise `no such module: vec0` and the vector index silently stays disabled. The migration code already logs `logger.warning("sqlite-vec not available; vector index disabled")` on failure and falls back to brute-force cosine — that fallback behavior is unchanged.

- [ ] **Step 5: Implement migration to v2**

Add method `_migrate_to_memory_v2` to `SQLiteStorage`:

```python
def _migrate_to_memory_v2(self, embedder_dim: int = 384) -> None:
    """Idempotently migrate session_chunks for hit tracking + create vec tables."""
    conn = self._get_conn()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= 2:
        return

    # Add hit_count and last_accessed columns
    for col, defn in [
        ("hit_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_accessed", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE session_chunks ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass

    # Create vec virtual tables
    try:
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS session_chunks_vec
            USING vec0(rowid INTEGER PRIMARY KEY, embedding FLOAT[{embedder_dim}])
        """)
    except Exception:
        logger.warning("sqlite-vec not available; vector index disabled")

    # Backfill existing embeddings
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM session_chunks_vec"
        ).fetchone()[0]
        if count == 0:
            conn.execute("""
                INSERT INTO session_chunks_vec (rowid, embedding)
                SELECT id, embedding FROM session_chunks
            """)
    except Exception:
        pass

    # Create knowledge vec table
    try:
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_vec
            USING vec0(rowid INTEGER PRIMARY KEY, embedding FLOAT[{embedder_dim}])
        """)
    except Exception:
        pass

    conn.execute("PRAGMA user_version = 2")
    conn.commit()
```

- [ ] **Step 6: Call migration in _init_db**

At the end of `_init_db()` (after `conn.commit()`), add:

```python
self._migrate_to_memory_v2()
```

- [ ] **Step 7: Add _ensure_vec_dimension for vec dimension alignment/rebuild**

`_migrate_to_memory_v2` hardcodes `embedder_dim=384` and has no rebuild logic. The spec requires the vec-table dimension to follow `embedder.dimension`, and to drop + rebuild the vec tables (re-indexing from the parent-table BLOBs) when the embedder model changes. Add `_ensure_vec_dimension` to `SQLiteStorage` (this needs `import re` at the top of `storage.py`, alongside the `logging` import added in Step 4):

```python
def _ensure_vec_dimension(self, embedder_dim: int) -> None:
    """Ensure vec tables use the current embedder dimension.

    Reads each vec table's CREATE statement from sqlite_master and extracts the
    declared FLOAT[N] dimension with a regex. If a table is missing or its
    declared dimension != embedder_dim, DROP it and recreate it with
    FLOAT[embedder_dim], then re-index from the parent tables' embedding BLOBs.
    Any failure (e.g. sqlite-vec unavailable) is swallowed so callers fall back
    to brute-force retrieval.
    """
    conn = self._get_conn()
    for vec_table, parent_table in [
        ("session_chunks_vec", "session_chunks"),
        ("knowledge_chunks_vec", "knowledge_chunks"),
    ]:
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (vec_table,),
            ).fetchone()
            declared = None
            if row and row[0]:
                m = re.search(r"FLOAT\[(\d+)\]", row[0])
                declared = int(m.group(1)) if m else None
            if declared == embedder_dim:
                continue
            # Drop and rebuild with the current dimension, then re-index
            conn.execute(f"DROP TABLE IF EXISTS {vec_table}")
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {vec_table}
                USING vec0(rowid INTEGER PRIMARY KEY, embedding FLOAT[{embedder_dim}])
            """)
            conn.execute(f"""
                INSERT INTO {vec_table} (rowid, embedding)
                SELECT id, embedding FROM {parent_table}
            """)
            conn.commit()
        except Exception:
            logger.warning(
                "Could not ensure %s dimension %s; using brute-force fallback",
                vec_table, embedder_dim,
            )
```

`LongTermMemory.__init__` (Task 4 Step 4) and `KnowledgeBase.__init__` (Task 5 Step 3) call this guarded by `hasattr(...)` so the `FakeStorage` test doubles — which lack this method — skip it safely.

- [ ] **Step 8: Extend query_chunks to return hit_count and last_accessed**

`query_chunks` currently returns only 8 columns, so the brute-force fallback path can never read the recency/hit data that `_score_and_rank` needs (`_score_and_rank` only reads `row[8]`/`row[9]` when `len(row) > 9`). Append the two columns — keeping the 8 original columns first, new ones at the end — so both the vec and brute-force paths return 10 columns:

```python
def query_chunks(self, limit: int = 15000) -> list[tuple]:
    """Return chunks ordered by recency, capped at `limit` rows.

    Returns: list of (id, session_id, commit_hash, chunk_type,
             chunk_text, payload_json, file_path, embedding,
             hit_count, last_accessed)
    """
    conn = self._get_conn()
    rows = conn.execute(
        """SELECT id, session_id, commit_hash, chunk_type,
                  chunk_text, payload_json, file_path, embedding,
                  hit_count, last_accessed
           FROM session_chunks
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [tuple(r) for r in rows]
```

`_score_and_rank` already has a `len(row) > 9` compatibility check, so it needs no change. A grep of `tests/` found no assertions on the old 8-column shape (no `len(row) == 8` or `rows[i][8]` checks on `query_chunks` results), so no test assertions need updating.

- [ ] **Step 9: Implement knowledge CRUD methods**

```python
def _vec_table_exists(self, name: str) -> bool:
    """Return True if the given virtual table exists in sqlite_master."""
    row = self._get_conn().execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None

def store_knowledge_chunks(self, source_path: str, chunks: list[dict]) -> None:
    """Transactionally replace all chunks for a source_path."""
    conn = self._get_conn()
    vec_exists = self._vec_table_exists("knowledge_chunks_vec")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if vec_exists:
            # Delete vec rows first (parent-table id subquery) so no orphan
            # vec rows remain; any failure below rolls back the whole
            # transaction, so there is no partial state.
            conn.execute(
                "DELETE FROM knowledge_chunks_vec WHERE rowid IN "
                "(SELECT id FROM knowledge_chunks WHERE source_path = ?)",
                (source_path,),
            )
        conn.execute(
            "DELETE FROM knowledge_chunks WHERE source_path = ?",
            (source_path,),
        )

        for c in chunks:
            conn.execute(
                """INSERT INTO knowledge_chunks
                   (source_path, chunk_index, chunk_text, embedding, metadata_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    source_path,
                    c["chunk_index"],
                    c["chunk_text"],
                    c["embedding_bytes"],
                    c.get("metadata_json", "{}"),
                ),
            )
        if vec_exists:
            conn.execute("""
                INSERT INTO knowledge_chunks_vec (rowid, embedding)
                SELECT id, embedding FROM knowledge_chunks
                WHERE source_path = ?
            """, (source_path,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

def delete_knowledge_chunks(self, source_path: str) -> None:
    conn = self._get_conn()
    if self._vec_table_exists("knowledge_chunks_vec"):
        conn.execute(
            "DELETE FROM knowledge_chunks_vec WHERE rowid IN "
            "(SELECT id FROM knowledge_chunks WHERE source_path = ?)",
            (source_path,),
        )
    conn.execute(
        "DELETE FROM knowledge_chunks WHERE source_path = ?",
        (source_path,),
    )
    conn.commit()

def query_knowledge_chunks(self, limit: int = 15000) -> list[tuple]:
    conn = self._get_conn()
    rows = conn.execute(
        """SELECT id, source_path, chunk_index, chunk_text,
                  metadata_json, embedding, hit_count, last_accessed
           FROM knowledge_chunks
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [tuple(r) for r in rows]

def query_knowledge_chunks_vec(
    self, query_embedding_bytes: bytes, limit: int
) -> list[tuple]:
    """sqlite-vec coarse search on knowledge chunks."""
    conn = self._get_conn()
    rows = conn.execute(
        """SELECT kc.id, kc.source_path, kc.chunk_index, kc.chunk_text,
                  kc.metadata_json, kc.embedding, kc.hit_count, kc.last_accessed,
                  vec.distance
           FROM knowledge_chunks_vec vec
           JOIN knowledge_chunks kc ON kc.id = vec.rowid
           WHERE vec.embedding MATCH ?
           ORDER BY vec.distance
           LIMIT ?""",
        (query_embedding_bytes, limit),
    ).fetchall()
    return [tuple(r) for r in rows]

def upsert_knowledge_meta(
    self, source_path: str, source_type: str, file_hash: str | None,
    chunk_count: int,
) -> None:
    conn = self._get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO knowledge_meta
           (source_path, source_type, file_hash, chunk_count, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))""",
        (source_path, source_type, file_hash, chunk_count),
    )
    conn.commit()

def delete_knowledge_meta(self, source_path: str) -> None:
    conn = self._get_conn()
    conn.execute("DELETE FROM knowledge_meta WHERE source_path = ?", (source_path,))
    conn.commit()

def list_knowledge_meta(self) -> list[dict]:
    conn = self._get_conn()
    rows = conn.execute(
        "SELECT * FROM knowledge_meta ORDER BY source_path"
    ).fetchall()
    return [dict(r) for r in rows]

def update_chunk_hit_counts(self, chunk_ids: list[int]) -> None:
    conn = self._get_conn()
    conn.executemany(
        """UPDATE session_chunks
           SET hit_count = hit_count + 1,
               last_accessed = datetime('now')
           WHERE id = ?""",
        [(cid,) for cid in chunk_ids],
    )
    conn.commit()

def query_chunks_vec(
    self, query_embedding_bytes: bytes, limit: int, dimension: int
) -> list[tuple] | None:
    """sqlite-vec coarse search. Returns None if vec unavailable."""
    conn = self._get_conn()
    try:
        rows = conn.execute(
            """SELECT sc.id, sc.session_id, sc.commit_hash, sc.chunk_type,
                      sc.chunk_text, sc.payload_json, sc.file_path, sc.embedding,
                      sc.hit_count, sc.last_accessed, vec.distance
               FROM session_chunks_vec vec
               JOIN session_chunks sc ON sc.id = vec.rowid
               WHERE vec.embedding MATCH ?
               ORDER BY vec.distance
               LIMIT ?""",
            (query_embedding_bytes, limit),
        ).fetchall()
        return [tuple(r) for r in rows]
    except Exception:
        return None
```

- [ ] **Step 10: Keep session_chunks_vec in sync with session_chunks**

`store_chunks` only writes `session_chunks`; the vec table is backfilled once during migration (Step 5), so any chunk stored afterwards would be invisible to sqlite-vec search. Add best-effort vec sync to the three session-chunk mutators.

In `store_chunks`, inside the transaction after all `INSERT` statements complete (before `conn.commit()`), add:

```python
# Sync vec table (best-effort; skip if sqlite-vec unavailable)
try:
    conn.execute("""
        INSERT INTO session_chunks_vec (rowid, embedding)
        SELECT id, embedding FROM session_chunks
        WHERE session_id = ?
    """, (session_id,))
except Exception:
    pass
```

In `delete_session_chunks`, delete the matching vec rows **before** deleting the parent rows:

```python
try:
    conn.execute(
        "DELETE FROM session_chunks_vec WHERE rowid IN "
        "(SELECT id FROM session_chunks WHERE session_id = ?)",
        (session_id,),
    )
except Exception:
    pass
```

In `delete_old_sessions`, delete the matching vec rows **before** the parent-table DELETE. The `session_id IN (...)` subquery must be byte-identical to the one the parent `delete_old_sessions` uses (the snippet below mirrors it exactly — same `SELECT DISTINCT session_id, MIN(created_at) AS first_seen ... ORDER BY first_seen DESC`), otherwise the vec delete could target a different session set than the parent delete and leave orphan rows:

```python
try:
    conn.execute(
        "DELETE FROM session_chunks_vec WHERE rowid IN "
        "(SELECT id FROM session_chunks WHERE session_id IN "
        "(SELECT session_id FROM (SELECT DISTINCT session_id, "
        " MIN(created_at) AS first_seen FROM session_chunks "
        " GROUP BY session_id ORDER BY first_seen DESC LIMIT -1 OFFSET ?)))",
        (keep_count,),
    )
except Exception:
    pass
```

Order matters: always remove `session_chunks_vec` rows before the parent rows are deleted, otherwise the `rowid IN (SELECT id FROM session_chunks ...)` join no longer resolves.

- [ ] **Step 11: Run tests to verify they pass**

Run: `pytest tests/test_storage.py::TestMigrationV2 tests/test_storage.py::TestKnowledgeCRUD -v`
Expected: all PASS

- [ ] **Step 12: Commit**

```bash
git add live_edit/storage.py tests/test_storage.py
git commit -m "feat: add storage migration v2 and knowledge base CRUD"
```

---

### Task 3: ShortTermMemory (L1)

**Files:**
- Create: `live_edit/memory.py` (partial — ShortTermMemory only)
- Test: `tests/test_short_term.py` (create)

**Interfaces:**
- Consumes: `ShortTermConfig`
- Produces: `ShortTermMemory(config)` class with `manage(messages, round_num) -> (list[dict], str)`
- Produces: Internal `_strip_old_rounds(messages, keep_full)` and `_summarize_old_rounds(messages, keep_full, provider, summary_model)`

- [ ] **Step 1: Write tests for ShortTermMemory**

```python
# tests/test_short_term.py
import pytest
from live_edit.config import ShortTermConfig
from live_edit.memory import ShortTermMemory


def make_messages(rounds: int) -> list[dict]:
    """Build a realistic message sequence for N rounds."""
    msgs = []
    for i in range(rounds):
        msgs.append({
            "role": "assistant",
            "content": [
                {"type": "text", "text": f"Thinking about round {i}"},
                {"type": "tool_use", "id": f"t{i}", "name": "edit_file",
                 "input": {"path": f"file{i}.py", "old_string": "foo", "new_string": "bar"}},
            ]
        })
        msgs.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}",
                 "content": [{"type": "text", "text": '{"ok": true, "file": "file0.py"}'}]},
            ]
        })
    return msgs


class FakeProvider:
    """Minimal fake provider: records the call and returns a canned summary."""

    def __init__(self):
        self.called = False

    async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
        self.called = True
        return [{"type": "text", "text": "S: old rounds summarized."}]


class TestShortTermMemory:
    def test_noop_when_under_max_full_rounds(self):
        cfg = ShortTermConfig(max_full_rounds=3, max_stripped_rounds=7, max_summary_rounds=20)
        sm = ShortTermMemory(cfg)
        msgs = make_messages(2)  # 2 rounds = 4 messages
        result, summary = sm.manage(msgs, round_num=2)
        assert result is msgs  # same object, no mutation needed
        assert summary == ""

    def test_strips_old_rounds(self):
        cfg = ShortTermConfig(max_full_rounds=2, max_stripped_rounds=5, max_summary_rounds=20)
        sm = ShortTermMemory(cfg)
        msgs = make_messages(5)  # 10 messages total
        result, summary = sm.manage(msgs, round_num=5)
        # max_full_rounds=2: last 2 rounds stay full; older rounds stripped.
        # Messages are ordered assistant, user, assistant, user, ...
        assert summary == ""
        assert result[0] is msgs[0]          # oldest assistant message preserved as-is
        first_user = result[1]               # oldest user message -> tool results stripped
        assert isinstance(first_user["content"], str)
        assert "edit_file" in first_user["content"]

    def test_summarizes_old_rounds_via_async(self):
        import asyncio
        cfg = ShortTermConfig(max_full_rounds=1, max_stripped_rounds=2, max_summary_rounds=6)
        sm = ShortTermMemory(cfg)
        msgs = make_messages(6)
        provider = FakeProvider()
        # round_num must be <= max_summary_rounds to land in the summarize band
        # (max_stripped_rounds < round_num <= max_summary_rounds)
        result, summary = asyncio.run(sm.manage_async(msgs, round_num=6, provider=provider))
        assert provider.called is True
        assert summary.startswith("[会话摘要]")
        # old rounds beyond max_full_rounds are stripped (assistant text/tool_use dropped,
        # first user message becomes a short string)
        assert isinstance(result[1]["content"], str)

    def test_strip_format_includes_tool_name_and_path(self):
        cfg = ShortTermConfig(max_full_rounds=1, max_stripped_rounds=5, max_summary_rounds=20)
        sm = ShortTermMemory(cfg)
        msgs = make_messages(3)
        result, _ = sm.manage(msgs, round_num=3)
        first_user = result[1]
        assert "edit_file" in str(first_user["content"])
        assert "file0.py" in str(first_user["content"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_short_term.py -v`
Expected: FAIL — `live_edit.memory` module or `ShortTermMemory` not defined

- [ ] **Step 3: Create live_edit/memory.py with ShortTermMemory**

```python
"""Three-tier memory system: ShortTermMemory, LongTermMemory, KnowledgeBase, MemoryManager."""

import logging
from dataclasses import dataclass

from .config import ShortTermConfig

logger = logging.getLogger("live-edit.memory")


class ShortTermMemory:
    """L1: Session window management — strip or summarize old rounds.

    Threshold bands (absolute round counts, validated by ShortTermConfig):
    - round_num <= max_full_rounds: no-op
    - round_num <= max_stripped_rounds: strip old rounds
    - round_num <= max_summary_rounds (async + provider): summarize, else strip
    - round_num > max_summary_rounds: strip only (conversation too long to keep
      spending tokens on a summary every round)
    """

    def __init__(self, config: ShortTermConfig):
        self.config = config

    def manage(
        self, messages: list[dict], round_num: int
    ) -> tuple[list[dict], str]:
        """Manage the message window (sync). Returns (mutated_messages, summary_text).

        The sync version never summarizes (it has no provider): once
        round_num exceeds max_full_rounds it only strips old rounds.
        """
        cfg = self.config
        if round_num <= cfg.max_full_rounds:
            return messages, ""
        return self._strip_old_rounds(messages, cfg.max_full_rounds), ""

    async def manage_async(
        self, messages: list[dict], round_num: int, provider=None
    ) -> tuple[list[dict], str]:
        """Async version with optional LLM summarization.

        Three bands (absolute round counts):
        - round_num <= max_full_rounds: no-op
        - round_num <= max_stripped_rounds: strip old rounds, summary=""
        - round_num <= max_summary_rounds (with provider): try `_summarize`;
          on success return (stripped, summary); on failure fall back to strip
        - round_num > max_summary_rounds: strip only, no summary
        """
        cfg = self.config

        if round_num <= cfg.max_full_rounds:
            return messages, ""

        if round_num <= cfg.max_stripped_rounds:
            return self._strip_old_rounds(messages, cfg.max_full_rounds), ""

        if round_num > cfg.max_summary_rounds:
            return self._strip_old_rounds(messages, cfg.max_full_rounds), ""

        # max_stripped_rounds < round_num <= max_summary_rounds: try summarizing
        if provider is not None:
            try:
                summary = await self._summarize(messages, cfg.max_full_rounds, provider)
                if summary:
                    stripped = self._strip_old_rounds(messages, cfg.max_full_rounds)
                    return stripped, summary
            except Exception:
                logger.warning("L1 summarization failed, falling back to strip-only")

        return self._strip_old_rounds(messages, cfg.max_full_rounds), ""

    def _strip_old_rounds(
        self, messages: list[dict], keep_full: int
    ) -> list[dict]:
        """Keep last `keep_full` rounds full; strip older rounds to one-liners."""
        # Each round = assistant + user pair
        total_rounds = len(messages) // 2
        if total_rounds <= keep_full:
            return messages

        keep_msgs = keep_full * 2
        result = []
        # Process older messages (index 0 to len-keep_msgs-1)
        for i in range(len(messages) - keep_msgs):
            msg = messages[i]
            if msg["role"] == "user" and isinstance(msg.get("content"), list):
                # Strip tool_results to summary
                parts = []
                for block in msg["content"]:
                    if block.get("type") == "tool_result":
                        tool_id = block.get("tool_use_id", "")
                        text = ""
                        if isinstance(block.get("content"), list):
                            for c in block["content"]:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text = c.get("text", "")
                                    break
                        # Try to extract tool name from matching assistant message
                        tool_name = "tool"
                        for prev_msg in reversed(messages[:i]):
                            if prev_msg["role"] == "assistant" and isinstance(
                                prev_msg.get("content"), list
                            ):
                                for tb in prev_msg["content"]:
                                    if (
                                        isinstance(tb, dict)
                                        and tb.get("type") == "tool_use"
                                        and tb.get("id") == tool_id
                                    ):
                                        tool_name = tb.get("name", "tool")
                                        # Extract file path if available
                                        inp = tb.get("input", {})
                                        path = inp.get("path", "")
                                        if path:
                                            tool_name += f" {path}"
                                        break
                                break
                        # Count +/- from result text
                        import re
                        added = len(re.findall(r"^\+", text, re.MULTILINE))
                        removed = len(re.findall(r"^-", text, re.MULTILINE))
                        stat = f"+{added}/-{removed}" if (added or removed) else ""
                        parts.append(f"{tool_name} {stat}".strip())
                result.append({"role": "user", "content": "; ".join(parts)})
            else:
                result.append(msg)

        # Append the last keep_msgs messages unchanged
        result.extend(messages[-keep_msgs:])
        return result

    async def _summarize(
        self, messages: list[dict], keep_full: int, provider
    ) -> str:
        """Call LLM to summarize old rounds beyond keep_full."""
        keep_msgs = keep_full * 2
        old_messages = messages[:-keep_msgs] if keep_msgs > 0 else messages

        # Build a compact representation of old rounds
        lines = []
        for msg in old_messages:
            if msg["role"] == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "text":
                            lines.append(block["text"][:200])
                        elif block.get("type") == "tool_use":
                            lines.append(f"[tool:{block.get('name')}]")
                else:
                    lines.append(str(content)[:200])

        old_text = "\n".join(lines[-3000:])  # keep it compact

        summary_model = self.config.summary_model or ""
        result = await provider.call_with_tools(
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Summarize the following conversation history in 2-3 sentences "
                        "in the original language. Focus on: what was requested, "
                        "which files were modified, and the outcome.\n\n"
                        f"Conversation:\n{old_text}"
                    ),
                }
            ],
            tools=[],
            on_thinking=lambda t: None,
            on_text=lambda t: None,
        )
        if result is None:
            return ""
        for block in result:
            if block and block.get("type") == "text":
                return "[会话摘要] " + block.get("text", "").strip()
        return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_short_term.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add live_edit/memory.py tests/test_short_term.py
git commit -m "feat: add ShortTermMemory (L1) with window strip and summarization"
```

---

### Task 4: LongTermMemory (L2) — Refactored SessionMemory

**Files:**
- Modify: `live_edit/memory.py` (add LongTermMemory)
- Test: `tests/test_long_term.py` (create — extends test_session_memory.py patterns)

**Interfaces:**
- Consumes: `LongTermConfig`, `SQLiteStorage`, `Embedder`
- Produces: `LongTermMemory(storage, embedder, config)` with `retrieve(query) -> list[MemoryEntry]` and `store(session_id, request, files, diff, commit_hash)`
- Produces: `MemoryEntry` dataclass (moved from session_memory.py)

- [ ] **Step 1: Write tests for LongTermMemory**

```python
# tests/test_long_term.py
import json
import math
import struct
import time
from unittest.mock import MagicMock, patch

import pytest
from live_edit.config import LongTermConfig
from live_edit.memory import LongTermMemory, MemoryEntry


class FakeEmbedder:
    """Returns simple deterministic embeddings for testing."""
    def __init__(self, dim=384):
        self._dim = dim

    def embed(self, text: str) -> list[float]:
        # Simple hash-based embedding for deterministic testing
        h = abs(hash(text)) % 1000
        return [h / 1000.0] * self._dim

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    @property
    def dimension(self) -> int:
        return self._dim


class FakeStorage:
    """In-memory storage that mimics the SQLiteStorage interface needed by L2."""
    def __init__(self):
        self.chunks = []
        self._next_id = 1

    def _get_conn(self):
        return self

    def query_chunks(self, limit: int = 15000) -> list[tuple]:
        return [(c["id"], c["session_id"], c["commit_hash"], c["chunk_type"],
                 c["chunk_text"], c["payload_json"], c.get("file_path", ""),
                 c["embedding_bytes"], c.get("hit_count", 0),
                 c.get("last_accessed", None))
                for c in self.chunks[-limit:]]

    def query_chunks_vec(self, query_emb: bytes, limit: int, dim: int):
        return None  # fallback to brute-force

    def update_chunk_hit_counts(self, chunk_ids: list[int]) -> None:
        for c in self.chunks:
            if c["id"] in chunk_ids:
                c["hit_count"] = c.get("hit_count", 0) + 1
                c["last_accessed"] = "2026-08-03T00:00:00"

    def store_chunks(self, session_id: str, commit_hash: str, chunks: list[dict]) -> None:
        # Remove old chunks for this session
        self.chunks = [c for c in self.chunks if c["session_id"] != session_id]
        for ch in chunks:
            self.chunks.append({
                "id": self._next_id,
                "session_id": session_id,
                "commit_hash": commit_hash,
                "chunk_type": ch["chunk_type"],
                "chunk_text": ch["chunk_text"],
                "payload_json": ch["payload_json"],
                "file_path": ch.get("file_path", ""),
                "embedding_bytes": ch["embedding_bytes"],
                "hit_count": 0,
                "last_accessed": None,
            })
            self._next_id += 1

    def delete_old_sessions(self, keep_count: int) -> None:
        sessions = list(dict.fromkeys(
            c["session_id"] for c in sorted(self.chunks, key=lambda c: c["id"])
        ))
        if len(sessions) > keep_count:
            to_delete = set(sessions[:-keep_count])
            self.chunks = [c for c in self.chunks if c["session_id"] not in to_delete]


class TestLongTermMemory:
    def test_retrieve_returns_similar_chunks(self):
        cfg = LongTermConfig(
            enabled=True,
            max_entries=5,
            similarity_threshold=0.0,  # accept everything for test
            recency_decay_rate=0.0,    # no decay
            hit_count_weight=0.0,      # no hit bonus
        )
        storage = FakeStorage()
        embedder = FakeEmbedder(dim=384)
        ltm = LongTermMemory(storage, embedder, cfg)

        # Store some chunks
        emb = struct.pack("384f", *[0.5] * 384)
        storage.store_chunks("s1", "abc", [
            {"chunk_type": "request", "chunk_text": "fix login bug",
             "payload_json": json.dumps({"request": "fix login bug"}),
             "file_path": "", "embedding_bytes": emb},
        ])
        storage.store_chunks("s2", "def", [
            {"chunk_type": "request", "chunk_text": "add navbar",
             "payload_json": json.dumps({"request": "add navbar"}),
             "file_path": "", "embedding_bytes": emb},
        ])

        results = ltm.retrieve_sync("fix auth bug")
        assert len(results) > 0

    def test_store_creates_chunks(self):
        cfg = LongTermConfig(enabled=True)
        storage = FakeStorage()
        embedder = FakeEmbedder(dim=384)
        ltm = LongTermMemory(storage, embedder, cfg)

        import asyncio
        asyncio.run(ltm.store(
            "s1", "update README", ["README.md"],
            "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n",
            "abc123",
        ))

        assert len(storage.chunks) > 0
        assert any(c["chunk_type"] == "request" for c in storage.chunks)
        assert any(c["chunk_type"] == "file_diff" for c in storage.chunks)

    def test_recency_decay_reduces_old_scores(self):
        cfg = LongTermConfig(
            enabled=True,
            similarity_threshold=0.0,
            recency_decay_rate=1.0,   # strong decay
            hit_count_weight=0.0,
            max_entries=5,
        )
        storage = FakeStorage()
        embedder = FakeEmbedder(dim=384)
        ltm = LongTermMemory(storage, embedder, cfg)

        emb = struct.pack("384f", *[0.9] * 384)
        storage.store_chunks("old_session", "oldhash", [
            {"chunk_type": "request", "chunk_text": "old edit",
             "payload_json": json.dumps({"request": "old edit"}),
             "file_path": "", "embedding_bytes": emb},
        ])
        # Set last_accessed to 100 days ago
        storage.chunks[0]["last_accessed"] = "2026-04-25T00:00:00"

        results = ltm.retrieve_sync("old edit")
        # Score should be heavily decayed by exp(-1.0 * 100) ≈ 0
        if results:
            assert results[0].score < 0.1

    def test_hit_count_gives_bonus(self):
        cfg = LongTermConfig(
            enabled=True,
            similarity_threshold=0.0,
            recency_decay_rate=0.0,
            hit_count_weight=0.05,
            max_entries=5,
        )
        storage = FakeStorage()
        embedder = FakeEmbedder(dim=384)
        ltm = LongTermMemory(storage, embedder, cfg)

        emb = struct.pack("384f", *[0.5] * 384)
        storage.store_chunks("s1", "abc", [
            {"chunk_type": "request", "chunk_text": "popular edit",
             "payload_json": json.dumps({"request": "popular edit"}),
             "file_path": "", "embedding_bytes": emb},
        ])
        storage.chunks[0]["hit_count"] = 10  # max bonus

        results = ltm.retrieve_sync("popular edit")
        if results:
            # Score should include hit bonus
            assert results[0].score > 0.4  # base ~0.5 + 0.05*min(10,10)

    def test_hit_count_capped_at_10(self):
        cfg = LongTermConfig(
            enabled=True,
            similarity_threshold=0.0,
            recency_decay_rate=0.0,
            hit_count_weight=0.05,
            max_entries=5,
        )
        storage = FakeStorage()
        embedder = FakeEmbedder(dim=384)
        ltm = LongTermMemory(storage, embedder, cfg)

        emb = struct.pack("384f", *[0.5] * 384)
        storage.store_chunks("s1", "abc", [
            {"chunk_type": "request", "chunk_text": "viral edit",
             "payload_json": json.dumps({"request": "viral edit"}),
             "file_path": "", "embedding_bytes": emb},
        ])
        storage.chunks[0]["hit_count"] = 50  # way over cap

        results = ltm.retrieve_sync("viral edit")
        if results:
            # Bonus should be 0.05 * 10 = 0.5, not 0.05 * 50 = 2.5
            assert results[0].score <= 1.0 + 0.5 + 0.05  # within bounds
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_long_term.py -v`
Expected: FAIL — `LongTermMemory` not defined

- [ ] **Step 3: Add module imports to memory.py**

`memory.py` already exists from Task 3 (it holds `ShortTermMemory` with only minimal imports). `LongTermMemory` in Step 4 uses `struct`, `math`, and `json`, so the module needs its full import set before the class is implemented. Replace the import block at the top of `memory.py` with:

```python
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import struct
from dataclasses import dataclass

from .config import ShortTermConfig, LongTermConfig, KnowledgeConfig

logger = logging.getLogger("live-edit.memory")
```

This becomes the single source of truth for `memory.py`: `LongTermMemory` (this task), `KnowledgeBase` (Task 5), and `MemoryManager` (Task 6) all rely on these imports. Task 5 Step 4 is now a verification step rather than a re-add.

- [ ] **Step 4: Implement LongTermMemory in memory.py**

Port the existing `SessionMemory` logic from `live_edit/session_memory.py` into `live_edit/memory.py`, with these changes:

1. `MemoryEntry` dataclass stays the same — copy to `memory.py`.
2. `__init__` takes `config: LongTermConfig` instead of `config: SessionMemoryConfig`.
3. Add `retrieve_sync(query)` method for sync retrieval (used in tests and as fallback).
4. `retrieve()` (async) calls `retrieve_sync()` via `run_in_executor`, then tries sqlite-vec path.
5. Add the recency decay + hit count scoring in `_score_and_rank`.
6. Add `update_chunk_hit_counts` call after retrieval.
7. Add a vec-dimension guard in `__init__` so L2 rebuilds the vec tables when the embedder dimension changes (method defined in Task 2 Step 7):

```python
# at the end of LongTermMemory.__init__ (after self._storage / self._embedder / self.config)
if hasattr(self._storage, "_ensure_vec_dimension"):
    try:
        self._storage._ensure_vec_dimension(self._embedder.dimension)
    except Exception:
        logger.warning("vec dimension check failed; using brute-force fallback")
```

Key new logic in `_score_and_rank`:

```python
def _score_and_rank(self, query_vec: list[float], rows: list[tuple]) -> list[MemoryEntry]:
    dim = len(query_vec)
    now_dt = None
    scored = []
    matched_ids = []

    for row in rows:
        session_id = row[1]
        commit_hash = row[2]
        chunk_type = row[3]
        file_path = row[6] if len(row) > 6 else ""
        emb_bytes = row[7] if len(row) > 7 else b""
        chunk_id = row[0]

        stored_vec = struct.unpack(f"{dim}f", emb_bytes)
        cosine = self._cosine_similarity(query_vec, stored_vec)
        if cosine < self.config.similarity_threshold:
            continue

        # Recency decay
        last_accessed = None
        hit_count = 0
        if len(row) > 9:
            hit_count = row[8] or 0
            last_accessed = row[9]

        if self.config.recency_decay_rate > 0 and last_accessed:
            from datetime import datetime, timezone
            if now_dt is None:
                now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
            try:
                accessed_dt = datetime.fromisoformat(
                    last_accessed.replace("Z", "+00:00")
                )
                if accessed_dt.tzinfo is not None:
                    accessed_dt = accessed_dt.astimezone(timezone.utc).replace(tzinfo=None)
                days = (now_dt - accessed_dt).total_seconds() / 86400
                decay = math.exp(-self.config.recency_decay_rate * days)
            except (ValueError, TypeError):
                decay = 1.0
        else:
            decay = 1.0

        # Hit count bonus (capped at 10)
        hit_bonus = self.config.hit_count_weight * min(hit_count, 10)

        final_score = cosine * decay + hit_bonus

        try:
            payload = json.loads(row[5])
        except (json.JSONDecodeError, TypeError):
            payload = {}

        scored.append((
            session_id, chunk_type, file_path, final_score,
            payload.get("request", ""), payload.get("stat", ""),
            commit_hash, payload.get("diff", ""),
        ))
        matched_ids.append(chunk_id)

    # Update hit counts for matched chunks
    if matched_ids:
        try:
            self._storage.update_chunk_hit_counts(matched_ids)
        except Exception:
            pass

    # Group by session, pick top-2, prefer file_diff (same logic as existing)
    by_session: dict[str, list] = {}
    for item in scored:
        sid = item[0]
        by_session.setdefault(sid, []).append(item)

    entries = []
    for _sid, items in by_session.items():
        items.sort(key=lambda x: x[3] + (0.0 if x[1] == "file_diff" else -0.05), reverse=True)
        for item in items[:2]:
            _sid, _ct, fpath, score, req, stat, chash, diff = item
            diff_lines = [line for line in (diff or "").split("\n") if line.strip()][:4]
            entries.append(MemoryEntry(
                session_id=_sid, request=req, file_path=fpath,
                diff_summary="\n".join(diff_lines), stat=stat,
                commit_hash=chash, score=score,
            ))

    entries.sort(key=lambda e: e.score, reverse=True)
    return entries[:self.config.max_entries]
```

Add `retrieve_sync` method:

```python
def retrieve_sync(self, query: str) -> list[MemoryEntry]:
    """Synchronous retrieval (used via run_in_executor and for tests)."""
    if not self.config.enabled:
        return []
    try:
        query_vec = self._embedder.embed(query)
        # Try sqlite-vec first
        dim = len(query_vec)
        query_bytes = struct.pack(f"{dim}f", *query_vec)
        vec_rows = self._storage.query_chunks_vec(
            query_bytes, self.config.coarse_recall_limit, dim
        )
        if vec_rows is not None:
            rows = [tuple(r) for r in vec_rows]
        else:
            rows = self._storage.query_chunks(limit=15000)
        return self._score_and_rank(query_vec, rows)
    except Exception:
        logger.warning("Failed to retrieve session memories", exc_info=True)
        return []
```

Async `retrieve` delegates to `retrieve_sync`:

```python
async def retrieve(self, query: str) -> list[MemoryEntry]:
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, self.retrieve_sync, query)
```

Copy `store`, `_split_diff_by_file`, `_migrate_if_needed`, and `_cosine_similarity` methods from `session_memory.py` — they remain functionally identical. Only update the `_migrate_if_needed` version check: call `self._storage._migrate_to_memory_v2()` instead of the old PRAGMA user_version 1 logic.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_long_term.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add live_edit/memory.py tests/test_long_term.py
git commit -m "feat: add LongTermMemory (L2) with sqlite-vec, recency decay, and hit count"
```

---

### Task 5: KnowledgeBase (L3)

**Files:**
- Modify: `live_edit/memory.py` (add KnowledgeBase)
- Test: `tests/test_knowledge.py` (create)

**Interfaces:**
- Consumes: `KnowledgeConfig`, `SQLiteStorage`, `Embedder`
- Produces: `KnowledgeBase(storage, embedder, config)` with `sync_files(project_root)`, `search(query)`, `add_api_document(source_path, content, metadata)`, `delete_document(source_path)`, `list_documents()`
- Produces: `KnowledgeEntry` dataclass

- [ ] **Step 1: Write tests for KnowledgeBase**

```python
# tests/test_knowledge.py
import json
import os
import struct
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from live_edit.config import KnowledgeConfig
from live_edit.memory import KnowledgeBase, KnowledgeEntry


class FakeEmbedder:
    def __init__(self, dim=384):
        self._dim = dim

    def embed(self, text: str) -> list[float]:
        return [0.5] * self._dim

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.5] * self._dim for _ in texts]

    @property
    def dimension(self) -> int:
        return self._dim


class TestKnowledgeBase:
    @pytest.fixture
    def kb(self, tmp_path):
        from live_edit.storage import SQLiteStorage
        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))
        embedder = FakeEmbedder(dim=384)
        cfg = KnowledgeConfig(enabled=True, knowledge_dir=str(tmp_path / "knowledge"))
        return KnowledgeBase(store, embedder, cfg)

    def test_chunk_split_respects_chunk_size(self, kb):
        text = "word " * 300  # ~1500 chars
        chunks = kb._split_text(text, chunk_size=500, overlap=50)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 500 + 100  # tolerance for word boundaries

    def test_sync_files_adds_and_detects_changes(self, kb, tmp_path):
        kb_dir = tmp_path / "knowledge"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "doc1.md").write_text("# Test Doc\n\nThis is a test document.")

        result = kb.sync_files(str(tmp_path))
        assert result["added"] == 1

        meta = kb._storage.list_knowledge_meta()
        assert len(meta) == 1

        # Modify the file
        (kb_dir / "doc1.md").write_text("# Updated Doc\n\nCompletely different content.")
        result2 = kb.sync_files(str(tmp_path))
        assert result2["updated"] == 1

    def test_sync_files_removes_deleted_files(self, kb, tmp_path):
        kb_dir = tmp_path / "knowledge"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "temp.md").write_text("temporary")

        kb.sync_files(str(tmp_path))
        assert kb._storage.list_knowledge_meta()

        (kb_dir / "temp.md").unlink()
        result = kb.sync_files(str(tmp_path))
        assert result["removed"] == 1

    def test_search_returns_relevant_chunks(self, kb, tmp_path):
        kb_dir = tmp_path / "knowledge"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "style.md").write_text("Use 4-space indentation for all Python files.")

        kb.sync_files(str(tmp_path))
        results = kb.search("python indentation")
        assert len(results) > 0
        assert isinstance(results[0], KnowledgeEntry)
        assert "indentation" in results[0].chunk_text.lower()

    def test_add_api_document(self, kb):
        kb.add_api_document("api:rules", "All commits must be signed.", {"tag": "git"})
        meta = kb._storage.list_knowledge_meta()
        assert any(m["source_path"] == "api:rules" for m in meta)
        assert any(m["source_type"] == "api" for m in meta)

    def test_delete_api_document_works(self, kb):
        kb.add_api_document("api:tmp", "delete me", {})
        kb.delete_document("api:tmp")
        assert not kb._storage.list_knowledge_meta()

    def test_delete_file_document_rejected(self, kb, tmp_path):
        kb_dir = tmp_path / "knowledge"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "keep.md").write_text("important")
        kb.sync_files(str(tmp_path))

        with pytest.raises(ValueError, match="file"):
            kb.delete_document("keep.md")

    def test_list_documents(self, kb):
        kb.add_api_document("api:a", "content a", {})
        kb.add_api_document("api:b", "content b", {})
        docs = kb.list_documents()
        paths = {d["source_path"] for d in docs}
        assert "api:a" in paths
        assert "api:b" in paths
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_knowledge.py -v`
Expected: FAIL — `KnowledgeBase` not defined

- [ ] **Step 3: Implement KnowledgeBase in memory.py**

```python
import hashlib
import os
import re
from dataclasses import dataclass


@dataclass
class KnowledgeEntry:
    source_path: str
    chunk_text: str
    score: float


class KnowledgeBase:
    """L3: Project knowledge base — file sync + API upload, independent of sessions."""

    def __init__(self, storage, embedder, config: KnowledgeConfig):
        self._storage = storage
        self._embedder = embedder
        self.config = config
        # Rebuild vec tables if the embedder dimension changed (Task 2 Step 7).
        # hasattr guard keeps test doubles (FakeStorage) that lack the method safe.
        if hasattr(self._storage, "_ensure_vec_dimension"):
            try:
                self._storage._ensure_vec_dimension(self._embedder.dimension)
            except Exception:
                logger.warning("vec dimension check failed; using brute-force fallback")

    # --- File Sync ---

    def sync_files(self, project_root: str) -> dict[str, int]:
        """Scan knowledge_dir, diff against meta, sync chunks. Returns change counts."""
        knowledge_dir = os.path.join(project_root, self.config.knowledge_dir)
        result = {"added": 0, "updated": 0, "removed": 0}

        # Collect files on disk
        disk_files: dict[str, str] = {}
        if os.path.isdir(knowledge_dir):
            for fname in os.listdir(knowledge_dir):
                if fname.endswith((".md", ".txt")):
                    fpath = os.path.join(knowledge_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            content = f.read()
                        disk_files[fname] = content
                    except Exception as e:
                        logger.warning("Failed to read knowledge file %s: %s", fpath, e)

        # Collect existing meta
        existing_meta = {
            m["source_path"]: m for m in self._storage.list_knowledge_meta()
            if m["source_type"] == "file"
        }

        # Find added/updated
        for fname, content in disk_files.items():
            file_hash = hashlib.sha256(content.encode()).hexdigest()
            if fname not in existing_meta:
                self._index_document(fname, content, "file", file_hash)
                result["added"] += 1
            elif existing_meta[fname].get("file_hash") != file_hash:
                self._index_document(fname, content, "file", file_hash)
                result["updated"] += 1

        # Find removed
        for fname in existing_meta:
            if fname not in disk_files:
                self._storage.delete_knowledge_chunks(fname)
                self._storage.delete_knowledge_meta(fname)
                result["removed"] += 1

        return result

    def _index_document(
        self, source_path: str, content: str, source_type: str, file_hash: str | None
    ) -> None:
        """Chunk, embed, and store a document."""
        chunks_text = self._split_text(content, self.config.chunk_size, self.config.chunk_overlap)
        embeddings = self._embedder.embed_batch(chunks_text)
        dim = self._embedder.dimension

        chunk_dicts = []
        for i, (text, vec) in enumerate(zip(chunks_text, embeddings)):
            chunk_dicts.append({
                "source_path": source_path,
                "chunk_index": i,
                "chunk_text": text,
                "embedding_bytes": struct.pack(f"{dim}f", *vec),
                "metadata_json": json.dumps({}, ensure_ascii=False),
            })

        self._storage.store_knowledge_chunks(source_path, chunk_dicts)
        self._storage.upsert_knowledge_meta(
            source_path, source_type, file_hash, len(chunk_dicts)
        )

    @staticmethod
    def _split_text(text: str, chunk_size: int, overlap: int) -> list[str]:
        """Split text into overlapping chunks, preferring paragraph boundaries."""
        paragraphs = re.split(r"\n\s*\n", text)
        chunks = []
        current = ""
        for para in paragraphs:
            if len(current) + len(para) <= chunk_size:
                current += ("\n\n" + para) if current else para
            else:
                if current:
                    chunks.append(current)
                # If a single paragraph is too long, split by sentences or fixed size
                if len(para) > chunk_size:
                    for i in range(0, len(para), chunk_size - overlap):
                        chunks.append(para[i:i + chunk_size])
                else:
                    current = para
        if current:
            chunks.append(current)
        return chunks

    # --- Search ---

    def search(self, query: str) -> list[KnowledgeEntry]:
        """Vector search on knowledge chunks."""
        if not self.config.enabled:
            return []
        try:
            query_vec = self._embedder.embed(query)
            dim = len(query_vec)
            query_bytes = struct.pack(f"{dim}f", *query_vec)

            # Try vec-based search
            try:
                rows = self._storage.query_knowledge_chunks_vec(
                    query_bytes, self.config.max_entries
                )
            except Exception:
                rows = []

            if not rows:
                # Fallback: brute-force
                rows = self._storage.query_knowledge_chunks(limit=15000)

            entries = []
            for row in rows:
                source_path = row[1]
                chunk_text = row[3]
                emb_bytes = row[5]
                stored_vec = struct.unpack(f"{dim}f", emb_bytes)
                score = self._cosine_similarity(query_vec, stored_vec)
                entries.append(KnowledgeEntry(
                    source_path=source_path,
                    chunk_text=chunk_text,
                    score=score,
                ))

            entries.sort(key=lambda e: e.score, reverse=True)
            return entries[:self.config.max_entries]
        except Exception:
            logger.warning("Knowledge base search failed", exc_info=True)
            return []

    @staticmethod
    def _cosine_similarity(a, b) -> float:
        import math
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    # --- API Document Management ---

    def add_api_document(
        self, source_path: str, content: str, metadata: dict
    ) -> None:
        """Add or update an API-uploaded document."""
        if not source_path.startswith("api:"):
            raise ValueError("API document source_path must start with 'api:'")
        self._index_document(
            source_path, content, "api",
            file_hash=hashlib.sha256(content.encode()).hexdigest(),
        )

    def delete_document(self, source_path: str) -> None:
        """Delete a document. Rejects file-type documents (managed by sync_files)."""
        meta_list = self._storage.list_knowledge_meta()
        meta = next((m for m in meta_list if m["source_path"] == source_path), None)
        if meta and meta["source_type"] == "file":
            raise ValueError(
                f"Cannot delete file-managed document '{source_path}' via API. "
                "Remove the file from the knowledge directory instead."
            )
        self._storage.delete_knowledge_chunks(source_path)
        self._storage.delete_knowledge_meta(source_path)

    def list_documents(self) -> list[dict]:
        """List all knowledge documents with metadata."""
        return self._storage.list_knowledge_meta()
```

- [ ] **Step 4: Verify memory.py imports are complete**

The complete import block was already added in Task 4 Step 3, so `memory.py` should already have `hashlib`, `json`, `logging`, `math`, `os`, `re`, `struct`, `asyncio`, `dataclass`, and the config imports. This step is a verification pass: `KnowledgeBase._split_text` uses `re` and `os`, `_index_document` uses `hashlib`, `struct`, and `json`, and `_cosine_similarity` uses `math` — confirm all of these are present in the top-of-file import block (they are). If any are missing, add them to the block.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_knowledge.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add live_edit/memory.py tests/test_knowledge.py
git commit -m "feat: add KnowledgeBase (L3) with file sync and API document management"
```

---

### Task 6: MemoryManager — Unified Entry Point + L3 Router Endpoints

**Files:**
- Modify: `live_edit/memory.py` (add MemoryManager)
- Modify: `live_edit/router.py` (add knowledge endpoints)
- Test: `tests/test_memory_manager.py` (create)
- Test: `tests/test_memory_router.py` (create)

**Interfaces:**
- Consumes: `MemoryConfig`, `SQLiteStorage`, `Embedder`, `Provider` (optional, for L1 summarization)
- Produces: `MemoryManager(storage, embedder, config, provider=None)` with `retrieve(query, session_id, messages, round_num) -> str` and `store(session_id, request, files, diff, commit_hash)`
- Produces: Router endpoints `POST/DELETE/GET /live-edit/knowledge`

- [ ] **Step 1: Write MemoryManager tests**

```python
# tests/test_memory_manager.py
import json
import struct
from unittest.mock import MagicMock, patch

import pytest
from live_edit.config import MemoryConfig, ShortTermConfig, LongTermConfig, KnowledgeConfig
from live_edit.memory import MemoryManager, MemoryEntry, KnowledgeEntry


class FakeEmbedder:
    def __init__(self, dim=384):
        self._dim = dim
    def embed(self, text):
        return [0.5] * self._dim
    def embed_batch(self, texts):
        return [[0.5] * self._dim for _ in texts]
    @property
    def dimension(self):
        return self._dim


class FakeStorage:
    def __init__(self):
        self.chunks = []
        self.knowledge_chunks = []
        self.knowledge_meta = []
        self._next_id = 1

    def _get_conn(self):
        return self

    def query_chunks(self, limit=15000):
        return [(c["id"], c["session_id"], c["commit_hash"], c["chunk_type"],
                 c["chunk_text"], c["payload_json"], c.get("file_path", ""),
                 c["embedding_bytes"], c.get("hit_count", 0),
                 c.get("last_accessed", None))
                for c in self.chunks[-limit:]]

    def query_chunks_vec(self, query_emb, limit, dim):
        return None

    def update_chunk_hit_counts(self, chunk_ids):
        for c in self.chunks:
            if c["id"] in chunk_ids:
                c["hit_count"] = c.get("hit_count", 0) + 1
                c["last_accessed"] = "2026-08-03T00:00:00"

    def store_chunks(self, session_id, commit_hash, chunks):
        self.chunks = [c for c in self.chunks if c["session_id"] != session_id]
        for ch in chunks:
            self.chunks.append({"id": self._next_id, "session_id": session_id,
                                "commit_hash": commit_hash, **ch,
                                "hit_count": 0, "last_accessed": None})
            self._next_id += 1

    def delete_old_sessions(self, keep_count):
        pass

    def query_knowledge_chunks(self, limit=15000):
        return [(c["id"], c["source_path"], c["chunk_index"], c["chunk_text"],
                 c["metadata_json"], c["embedding_bytes"],
                 c.get("hit_count", 0), c.get("last_accessed", None))
                for c in self.knowledge_chunks]

    def query_knowledge_chunks_vec(self, query_emb, limit):
        return self.query_knowledge_chunks(limit)

    def list_knowledge_meta(self):
        return self.knowledge_meta


class TestMemoryManager:
    @pytest.fixture
    def mgr(self):
        cfg = MemoryConfig(
            enabled=True,
            short_term=ShortTermConfig(max_full_rounds=3, max_stripped_rounds=7, max_summary_rounds=20),
            long_term=LongTermConfig(enabled=True, similarity_threshold=0.0,
                                     recency_decay_rate=0.0, hit_count_weight=0.0, max_entries=5),
            knowledge=KnowledgeConfig(enabled=False),
        )
        storage = FakeStorage()
        embedder = FakeEmbedder(dim=384)
        return MemoryManager(storage, embedder, cfg)

    def test_retrieve_l1_noop_when_under_window(self, mgr):
        msgs = [{"role": "user", "content": "hello"}]
        context, updated_msgs = mgr.retrieve_sync("query", "s1", msgs, round_num=1)
        assert updated_msgs is msgs

    def test_retrieve_l2_includes_past_changes(self, mgr):
        emb = struct.pack("384f", *[0.5] * 384)
        mgr._storage.store_chunks("past_sess", "hash1", [
            {"chunk_type": "request", "chunk_text": "fix login bug",
             "payload_json": json.dumps({"request": "fix login bug"}),
             "file_path": "", "embedding_bytes": emb},
        ])

        msgs = [{"role": "user", "content": "fix auth"}]
        context, _ = mgr.retrieve_sync("fix auth", "s1", msgs, round_num=1)
        assert "Relevant Past Changes" in context or len(context) > 0

    def test_store_delegates_to_l2(self, mgr):
        mgr.store_sync("s1", "update readme", ["README.md"],
                       "diff --git a/README.md ...", "abc123")
        assert len(mgr._storage.chunks) > 0

    def test_disabled_master_switch_skips_all(self):
        cfg = MemoryConfig(enabled=False)
        mgr = MemoryManager(FakeStorage(), FakeEmbedder(dim=384), cfg)
        msgs = [{"role": "user", "content": "test"}]
        context, _ = mgr.retrieve_sync("test", "s1", msgs, round_num=10)
        assert "Relevant Past Changes" not in context
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_memory_manager.py -v`
Expected: FAIL — `MemoryManager` not defined or methods missing

- [ ] **Step 3: Implement MemoryManager**

```python
class MemoryManager:
    """Unified three-tier memory. Engine calls this single entry point."""

    def __init__(self, storage, embedder, config: MemoryConfig, provider=None):
        self.config = config
        self._provider = provider
        self._short_term = ShortTermMemory(config.short_term) if config.enabled else None
        self._long_term = (
            LongTermMemory(storage, embedder, config.long_term)
            if config.enabled and config.long_term.enabled
            else None
        )
        self._knowledge = (
            KnowledgeBase(storage, embedder, config.knowledge)
            if config.enabled and config.knowledge.enabled
            else None
        )

    def manage_messages(
        self, messages: list[dict], round_num: int
    ) -> tuple[list[dict], str]:
        """L1-only window management (strip/summarize). Returns (updated_messages, summary)."""
        if self._short_term is None:
            return messages, ""
        return self._short_term.manage(messages, round_num)

    async def manage_messages_async(
        self, messages: list[dict], round_num: int, provider=None
    ) -> tuple[list[dict], str]:
        """Async L1-only window management. Returns (updated_messages, summary)."""
        if self._short_term is None:
            return messages, ""
        return await self._short_term.manage_async(messages, round_num, provider)

    async def retrieve(
        self, query: str, session_id: str, messages: list[dict], round_num: int
    ) -> tuple[str, list[dict]]:
        """Return (context_string, updated_messages).

        context_string is injected into system prompt or appended as a message.
        updated_messages may have old rounds stripped/compressed by L1.
        """
        parts: list[str] = []
        updated_messages = messages

        # L1: window management
        if self._short_term is not None:
            updated_messages, summary = await self._short_term.manage_async(
                messages, round_num, self._provider
            )
            if summary:
                parts.append(summary)

        # L2: long-term memory
        if self._long_term is not None:
            memories = await self._long_term.retrieve(query)
            if memories:
                parts.append(_format_memory_context(
                    memories, self.config.long_term.memory_prompt_template
                ))
                memories_hit = True
            else:
                memories_hit = False
        else:
            memories_hit = False

        # L3: knowledge — fires when L2 is disabled or returned empty
        if self._knowledge is not None and not memories_hit:
            knowledge_entries = self._knowledge.search(query)
            if knowledge_entries:
                parts.append(_format_knowledge_context(knowledge_entries))

        context = "\n\n".join(parts)
        return context, updated_messages

    def retrieve_sync(
        self, query: str, session_id: str, messages: list[dict], round_num: int
    ) -> tuple[str, list[dict]]:
        """Synchronous version for testing."""
        parts: list[str] = []
        updated_messages = messages

        if self._short_term is not None:
            updated_messages, summary = self._short_term.manage(messages, round_num)
            if summary:
                parts.append(summary)

        if self._long_term is not None:
            memories = self._long_term.retrieve_sync(query)
            if memories:
                parts.append(_format_memory_context(
                    memories, self.config.long_term.memory_prompt_template
                ))
                memories_hit = True
            else:
                memories_hit = False
        else:
            memories_hit = False

        # L3: knowledge — fires when L2 is disabled or returned empty
        if self._knowledge is not None and not memories_hit:
            knowledge_entries = self._knowledge.search(query)
            if knowledge_entries:
                parts.append(_format_knowledge_context(knowledge_entries))

        return "\n\n".join(parts), updated_messages

    async def store(
        self, session_id: str, request: str, files: list[str],
        diff: str, commit_hash: str,
    ) -> None:
        """Store session in L2 long-term memory."""
        if self._long_term is not None:
            await self._long_term.store(session_id, request, files, diff, commit_hash)

    def store_sync(self, session_id: str, request: str, files: list[str],
                   diff: str, commit_hash: str) -> None:
        """Synchronous store for testing. Uses a fresh loop when none is running."""
        import asyncio
        if self._long_term is None:
            return
        coro = self._long_term.store(session_id, request, files, diff, commit_hash)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
        else:
            asyncio.get_running_loop().create_task(coro)

    def sync_knowledge_files(self, project_root: str) -> dict:
        """Sync L3 knowledge files. Called at startup."""
        if self._knowledge is not None:
            return self._knowledge.sync_files(project_root)
        return {}

    def add_knowledge(self, source_path: str, content: str, metadata: dict) -> None:
        if self._knowledge is None:
            raise RuntimeError("Knowledge base is not enabled")
        self._knowledge.add_api_document(source_path, content, metadata)

    def delete_knowledge(self, source_path: str) -> None:
        if self._knowledge is None:
            raise RuntimeError("Knowledge base is not enabled")
        self._knowledge.delete_document(source_path)

    def list_knowledge(self) -> list[dict]:
        if self._knowledge is None:
            return []
        return self._knowledge.list_documents()


def _format_memory_context(memories: list[MemoryEntry], template: str = "") -> str:
    """Format L2 MemoryEntry list for injection (ported from engine.py)."""
    if template:
        try:
            items = []
            for i, m in enumerate(memories, 1):
                items.append(
                    template.replace("{index}", str(i))
                    .replace("{request}", m.request)
                    .replace("{file}", m.file_path or "(request)")
                    .replace("{diff_summary}", m.diff_summary or "")
                    .replace("{stat}", m.stat or "")
                    .replace("{commit_hash}", m.commit_hash)
                    .replace("{score}", f"{min(m.score, 1.0):.0%}")
                )
            return "\n".join(items)
        except Exception:
            pass

    lines = [
        "## Relevant Past Changes",
        "",
        "Similar past edits (reference only, adapt to current request):",
        "",
    ]
    for i, m in enumerate(memories, 1):
        file_info = f" -> {m.file_path}" if m.file_path else ""
        # hit bonus may push score above 1.0; clamp for display
        lines.append(f'{i}. "{m.request}" ({min(m.score, 1.0):.0%}){file_info}')
        if m.diff_summary:
            summary_lines = m.diff_summary.strip().split("\n")[:3]
            for sl in summary_lines:
                lines.append(f"   {sl[:120]}")
        lines.append("")
    lines.append("Use the above as reference only -- do not blindly copy past solutions.")
    return "\n".join(lines)


def _format_knowledge_context(entries: list[KnowledgeEntry]) -> str:
    """Format L3 KnowledgeEntry list for injection."""
    lines = ["## Project Knowledge", ""]
    for e in entries:
        fname = e.source_path
        text = e.chunk_text[:300].replace("\n", " ")
        lines.append(f"- {fname}: \"{text}...\"")
    return "\n".join(lines)
```

- [ ] **Step 4: Add knowledge endpoints to router.py**

In `setup_live_edit()`, after the health endpoint, add:

```python
@router.post("/live-edit/knowledge")
async def upload_knowledge(
    source_path: str = Form(...),
    content: str = Form(...),
    metadata: str = Form("{}"),
) -> dict:
    """Upload a document snippet to the knowledge base."""
    import json as _json
    from .config import Config as _Config

    try:
        _meta = _json.loads(metadata)
    except _json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="metadata must be valid JSON")

    if not source_path.startswith("api:"):
        raise HTTPException(
            status_code=400,
            detail="source_path must start with 'api:' for API-uploaded documents",
        )

    mem_mgr = getattr(app.state, "memory_manager", None)
    if mem_mgr is None:
        raise HTTPException(status_code=503, detail="Memory system not available")

    try:
        mem_mgr.add_knowledge(source_path, content, _meta)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "source_path": source_path}


@router.delete("/live-edit/knowledge/{source_path:path}")
async def delete_knowledge(source_path: str) -> dict:
    """Delete an API-uploaded knowledge document."""
    mem_mgr = getattr(app.state, "memory_manager", None)
    if mem_mgr is None:
        raise HTTPException(status_code=503, detail="Memory system not available")

    try:
        mem_mgr.delete_knowledge(source_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True}


@router.get("/live-edit/knowledge")
async def list_knowledge() -> dict:
    """List all knowledge base documents."""
    mem_mgr = getattr(app.state, "memory_manager", None)
    if mem_mgr is None:
        return {"documents": []}
    return {"documents": mem_mgr.list_knowledge()}
```

Add `from fastapi import Form` to router imports if not already present.

- [ ] **Step 5: Write router tests**

```python
# tests/test_memory_router.py
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch


class TestKnowledgeEndpoints:
    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from live_edit.config import Config, MemoryConfig, KnowledgeConfig
        from live_edit.memory import MemoryManager

        app = FastAPI()

        class FakeEmbedder:
            def embed(self, text):
                return [0.5] * 384
            def embed_batch(self, texts):
                return [[0.5] * 384 for _ in texts]
            @property
            def dimension(self):
                return 384

        class FakeStorage:
            def _get_conn(self):
                return self
            def store_knowledge_chunks(self, source_path, chunks):
                pass
            def upsert_knowledge_meta(self, source_path, source_type, file_hash, chunk_count):
                pass
            def delete_knowledge_chunks(self, source_path):
                pass
            def delete_knowledge_meta(self, source_path):
                pass
            def list_knowledge_meta(self):
                return [
                    {"source_path": "api:test", "source_type": "api",
                     "file_hash": None, "chunk_count": 1,
                     "created_at": "2026-01-01", "updated_at": "2026-01-01"},
                ]

        storage = FakeStorage()
        embedder = FakeEmbedder()
        cfg = MemoryConfig(knowledge=KnowledgeConfig(enabled=True, api_enabled=True))
        mgr = MemoryManager(storage, embedder, cfg)

        from live_edit.router import setup_live_edit
        config = Config(memory=cfg)
        with patch("live_edit.router._resolve_api_key", return_value="test-key"):
            app.include_router(setup_live_edit(config))

        app.state.memory_manager = mgr

        return TestClient(app)

    def test_upload_knowledge(self, client):
        resp = client.post("/live-edit/knowledge", data={
            "source_path": "api:rules",
            "content": "All commits must be signed.",
            "metadata": '{"tag": "git"}',
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_upload_rejects_non_api_prefix(self, client):
        resp = client.post("/live-edit/knowledge", data={
            "source_path": "myfile.md",
            "content": "test",
            "metadata": "{}",
        })
        assert resp.status_code == 400

    def test_list_knowledge(self, client):
        resp = client.get("/live-edit/knowledge")
        assert resp.status_code == 200
        data = resp.json()
        assert "documents" in data

    def test_delete_knowledge(self, client):
        resp = client.delete("/live-edit/knowledge/api:test")
        assert resp.status_code == 200
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_memory_manager.py tests/test_memory_router.py -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add live_edit/memory.py live_edit/router.py tests/test_memory_manager.py tests/test_memory_router.py
git commit -m "feat: add MemoryManager and knowledge API endpoints"
```

---

### Task 7: Engine Integration

**Files:**
- Modify: `live_edit/engine.py`
- Test: `tests/test_engine.py` (extend)

**Interfaces:**
- Consumes: `MemoryManager` from `live_edit.memory`
- Modifies: `run_edit_session` setup block and memory retrieval/store calls
- Removes: direct `LocalEmbedder` + `SessionMemory` construction

- [ ] **Step 1: Write an engine integration test**

```python
# Add to tests/test_engine.py

def test_memory_manager_integration():
    """Verify engine constructs MemoryManager correctly when config.memory.enabled is True."""
    from live_edit.config import Config, MemoryConfig, LongTermConfig
    from live_edit.memory import MemoryManager

    config = Config(
        memory=MemoryConfig(
            enabled=True,
            long_term=LongTermConfig(enabled=True),
        ),
    )
    assert config.memory.enabled is True
    assert config.memory.long_term.enabled is True
    # Backward compat
    assert config.session_memory is config.memory.long_term
```

- [ ] **Step 2: Modify engine.py — replace SessionMemory setup**

In `run_edit_session()`, replace lines ~554-578 (the session memory setup block):

```python
# ── Memory system setup ──
from .embedder import LocalEmbedder
from .memory import MemoryManager, _format_memory_context

memory_manager = None
if config.memory.enabled:
    try:
        sm_config = config.memory.long_term
        if sm_config.enabled:
            sm_embedder = LocalEmbedder(model_name=sm_config.embedder.model)
            # Validate embedding works
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, sm_embedder.embed, "test")
        else:
            sm_embedder = LocalEmbedder(model_name="thenlper/gte-small")
    except ImportError:
        logger.warning(
            "memory.long_term.enabled=true but sentence-transformers is not "
            "installed. Install with: pip install live-edit[rag]"
        )
        sm_embedder = None
    except Exception as e:
        logger.warning("Memory embedder failed to load: %s", e)
        sm_embedder = None

    if sm_embedder is not None:
        memory_manager = MemoryManager(
            storage=storage,
            embedder=sm_embedder,
            config=config.memory,
            provider=provider,
        )
        # Sync knowledge files at startup
        if config.memory.knowledge.enabled and config.memory.knowledge.knowledge_dir:
            try:
                project_root = config.project.root or "."
                result = memory_manager.sync_knowledge_files(project_root)
                if any(v > 0 for v in result.values()):
                    logger.info("Knowledge base synced: %s", result)
            except Exception as e:
                logger.warning("Knowledge base sync failed: %s", e)

session._session_memory = memory_manager  # type: ignore[assignment]
```

- [ ] **Step 3: Replace retrieval calls in engine.py**

**Continue path** (around lines 594-603): Replace the `if session_memory is not None:` block:

```python
# ── Retrieve memory context for continuation ──
if memory_manager is not None:
    try:
        memory_context, messages = await memory_manager.retrieve(
            query=continue_msg,
            session_id=session.id,
            messages=messages,
            round_num=0,
        )
        if memory_context:
            messages.append({"role": "user", "content": memory_context})
    except Exception as e:
        logger.warning("Failed to retrieve memory for continuation: %s", e)
```

**New session path** (around lines 611-621): Replace the retrieval block. Pass `round_num=1` (not `0`) so L1 window management acts on the first round. `messages[0]` remains the system prompt and `messages[1]` the original user request; use the returned (possibly L1-stripped) `messages`:

```python
# ── Retrieve memory context ──
if memory_manager is not None and not continue_msg:
    try:
        memory_context, messages = await memory_manager.retrieve(
            query=session.request,
            session_id=session.id,
            messages=messages,
            round_num=1,  # L1 should act at least on the first round
        )
        if memory_context:
            system_prompt += "\n\n" + memory_context
            messages[0]["content"] = system_prompt
    except Exception as e:
        logger.warning("Failed to retrieve session memory: %s", e)
```

- [ ] **Step 4: Integrate L1 window management into the edit loop**

Inside `run_edit_session`'s `while round_num < max_rounds:` loop, run L1 window management right before each `provider.call_with_tools(...)` invocation, so the messages handed to the provider are stripped/summarized as the session grows:

```python
# Manage the L1 window before each provider turn.
if memory_manager is not None:
    try:
        messages, l1_summary = await memory_manager.manage_messages_async(
            messages, round_num, provider
        )
        if l1_summary:
            # Optionally surface the L1 summary into the context
            messages.append({
                "role": "user",
                "content": f"[Prior rounds summarized] {l1_summary}",
            })
    except Exception as e:
        logger.warning("L1 window management failed: %s", e)
# ... then call provider.call_with_tools(messages, ...) with the returned `messages`
```

The returned `messages` (not the pre-manage list) must be the one passed to `provider.call_with_tools` for this round.

- [ ] **Step 5: Replace store call in _do_commit**

Around line 443-454, replace:

```python
sm = getattr(session, "_session_memory", None)
if sm is not None:
    try:
        await sm.store(
            session_id=session.id,
            request=session.request,
            files=session._modified_files,
            diff=getattr(session, "_cached_diff", ""),
            commit_hash=session._commit_hash,
        )
    except Exception as e:
        logger.warning("Failed to store session memory: %s", e)
```

With:

```python
mem_mgr = getattr(session, "_session_memory", None)
if mem_mgr is not None:
    try:
        await mem_mgr.store(
            session_id=session.id,
            request=session.request,
            files=session._modified_files,
            diff=getattr(session, "_cached_diff", ""),
            commit_hash=session._commit_hash,
        )
    except Exception as e:
        logger.warning("Failed to store session memory: %s", e)
```

- [ ] **Step 6: Remove unused import**

Remove `from .session_memory import SessionMemory` from engine.py imports, and remove engine.py's local `_format_memory_context` helper (it is only used in the two retrieval blocks replaced in Steps 3-4 above). Add `_format_memory_context` to the existing `from .memory import MemoryManager` import so the calls in the replaced blocks resolve to memory.py's version.

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_engine.py -v -k "memory or test_memory"` and `pytest tests/test_engine.py -v`
Expected: all existing engine tests still pass

- [ ] **Step 8: Commit**

```bash
git add live_edit/engine.py tests/test_engine.py
git commit -m "feat: integrate MemoryManager into engine, replacing SessionMemory"
```

---

### Task 8: Backward Compat & Cleanup

**Files:**
- Modify: `live_edit/session_memory.py` (deprecate — re-export from memory.py)
- Modify: `pyproject.toml` (add sqlite-vec dependency)
- Modify: `docs/onboarding.md` (update docs)

- [ ] **Step 1: Deprecate session_memory.py**

Replace the content of `session_memory.py` with re-exports:

```python
"""Deprecated module — use live_edit.memory instead.

This module is kept for backward compatibility. All functionality has
been moved to `live_edit.memory`. Existing imports will continue to work
but will emit DeprecationWarning.
"""

import warnings

warnings.warn(
    "live_edit.session_memory is deprecated; use live_edit.memory instead",
    DeprecationWarning,
    stacklevel=2,
)

from .memory import (  # noqa: E402, F401
    KnowledgeBase,
    KnowledgeEntry,
    LongTermMemory,
    MemoryEntry,
    MemoryManager,
    ShortTermMemory,
)

# Keep the old name working
from .memory import LongTermMemory as SessionMemory  # noqa: E402, F401
```

- [ ] **Step 2: Update test_session_memory.py FakeStorage**

`tests/test_session_memory.py`'s `FakeStorage` (lines ~31-74) is missing the `query_chunks_vec` and `update_chunk_hit_counts` methods. After this task's re-export, `LongTermMemory.retrieve_sync` calls both on the fake; the resulting `AttributeError` is swallowed by `retrieve_sync`'s outer try/except, so retrieval returns `[]` and non-empty assertions such as `test_retrieve_finds_relevant_chunks` fail. Add the two methods to the fake:

```python
def query_chunks_vec(self, query_emb, limit, dim):
    return None  # fallback to brute-force cosine

def update_chunk_hit_counts(self, chunk_ids):
    pass  # fake does not persist hit tracking
```

No real hit tracking is needed in the fake: `LongTermMemory._score_and_rank` already wraps its `update_chunk_hit_counts` call in try/except, and `retrieve_sync` has an outer try/except, so a safe no-op method restores the existing `test_session_memory.py` cases to passing.

- [ ] **Step 3: Add sqlite-vec to pyproject.toml**

In `[project.optional-dependencies]`, add `sqlite-vec` to **both** the `dev` and `rag` extras. `dev` needs it so `test_backfills_vec_table` (Task 2 Step 1) can run locally instead of skipping; `rag` needs it so the vector index works in production:

```toml
dev = [
    "mypy>=1.10.0",
    "pytest>=7.0.0",
    "pytest-asyncio>=0.21.0",
    "pytest-cov>=5.0.0",
    "ruff>=0.6.0",
    "sqlite-vec>=0.1.0",
]
rag = ["sentence-transformers>=3.0", "sqlite-vec>=0.1.0"]
```

- [ ] **Step 4: Update onboarding docs**

In `docs/onboarding.md`, find the `[session_memory]` section and add a note about `[memory]`:

```markdown
### Session Memory → Memory System (v0.3.0+)

The `[session_memory]` section is deprecated in favor of `[memory]`:

```toml
[memory]
enabled = true

[memory.short_term]
max_full_rounds = 3

[memory.long_term]
enabled = true
embedder = { type = "local", model = "thenlper/gte-small" }

[memory.knowledge]
enabled = true
knowledge_dir = ".live-edit/knowledge"
```

The old `[session_memory]` section still works but maps to `[memory.long_term]`.
New features like recency decay and knowledge base are only available via `[memory]`.
```

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `pytest tests/ -v --ignore=tests/test_cli.py`
Expected: all tests pass (or pre-existing failures unrelated to this change)

- [ ] **Step 6: Commit**

```bash
git add live_edit/session_memory.py pyproject.toml docs/onboarding.md
git commit -m "chore: deprecate session_memory.py, add sqlite-vec dep, update docs"
```
