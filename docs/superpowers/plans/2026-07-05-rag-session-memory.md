# RAG Session Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add retrieval-augmented session memory so the agent automatically references similar past edit sessions when starting new ones.

**Architecture:** Two new pluggable modules (`Embedder` ABC + `SessionMemory`) following the existing Provider/Storage/VCS pattern. New abstract methods on `Storage` ABC for embedding CRUD. Engine modified to store embeddings on commit and inject memory context on new sessions and continuations.

**Tech Stack:** Python 3.10+, `sentence-transformers>=3.0` (optional), `struct` (stdlib), SQLite BLOB storage

---

### Task 1: Config Dataclasses

**Files:**
- Modify: `live_edit/config.py`

- [ ] **Step 1: Add `EmbedderConfig` and `SessionMemoryConfig` dataclasses**

Insert after the `EvaluationConfig` dataclass (after line 90):

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

- [ ] **Step 2: Add `session_memory` field to `Config` dataclass**

Add after the `evaluation` field in the `Config` dataclass (after line 124):

```python
    session_memory: SessionMemoryConfig = field(default_factory=SessionMemoryConfig)
```

- [ ] **Step 3: Parse `[session_memory]` section in `parse_config()`**

Add before `toml_tools = raw.get("tools", [])` (before line 241):

```python
    sm_data = raw.get("session_memory", {})
    sm_embedder_data = sm_data.get("embedder", {})
    sm_embedder = EmbedderConfig(
        type=sm_embedder_data.get("type", "local"),
        model=sm_embedder_data.get("model", "all-MiniLM-L6-v2"),
        api_url=sm_embedder_data.get("api_url", ""),
        api_key_env=sm_embedder_data.get("api_key_env", ""),
    )
    session_memory = SessionMemoryConfig(
        enabled=sm_data.get("enabled", False),
        max_entries=sm_data.get("max_entries", 10),
        similarity_threshold=sm_data.get("similarity_threshold", 0.6),
        max_stored_entries=sm_data.get("max_stored_entries", 5000),
        memory_prompt_template=sm_data.get("memory_prompt_template", ""),
        embedder=sm_embedder,
    )
```

- [ ] **Step 4: Pass `session_memory` into the `Config()` constructor**

In the `return Config(...)` call at the end of `parse_config()`, add after `evaluation=evaluation,`:

```python
        session_memory=session_memory,
```

- [ ] **Step 5: Test — verify Config parsing**

Run: `pytest tests/test_config.py -v -k "test_" 2>&1 | head -30`
Expected: existing tests pass. Then write a quick inline test:

```bash
python3 -c "
from live_edit.config import Config, SessionMemoryConfig, EmbedderConfig
c = Config()
assert c.session_memory.enabled == False
assert c.session_memory.max_entries == 10
assert isinstance(c.session_memory.embedder, EmbedderConfig)
print('OK')
"
```

- [ ] **Step 6: Commit**

```bash
git add live_edit/config.py
git commit -m "feat: add SessionMemoryConfig and EmbedderConfig dataclasses"
```

---

### Task 2: Embedder Interface + LocalEmbedder

**Files:**
- Create: `live_edit/embedder.py`
- Create: `tests/test_embedder.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_embedder.py`:

```python
"""Tests for live_edit.embedder — Embedder ABC and LocalEmbedder."""

import threading
import pytest
from unittest.mock import MagicMock, patch
from live_edit.embedder import Embedder, LocalEmbedder


class TestEmbedderABC:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            Embedder()

    def test_concrete_subclass_must_implement_embed_and_dimension(self):
        class Incomplete(Embedder):
            pass
        with pytest.raises(TypeError):
            Incomplete()

    def test_embed_batch_default_loops_embed(self):
        class SimpleEmbedder(Embedder):
            def embed(self, text):
                return [len(text), float(ord(text[0])) if text else 0.0]

            @property
            def dimension(self):
                return 2

        e = SimpleEmbedder()
        results = e.embed_batch(["hi", "bye"])
        assert len(results) == 2
        assert results[0] == [2.0, float(ord("h"))]
        assert results[1] == [3.0, float(ord("b"))]


class TestLocalEmbedder:
    @pytest.fixture
    def mock_sentence_transformer(self):
        with patch("live_edit.embedder.SentenceTransformer") as mock_st:
            mock_model = MagicMock()
            mock_model.encode.return_value = np.array([0.1, 0.2, 0.3], dtype=np.float32)
            mock_st.return_value = mock_model
            yield mock_st, mock_model

    def test_dimension_is_384(self, mock_sentence_transformer):
        mock_st, mock_model = mock_sentence_transformer
        mock_model.get_sentence_embedding_dimension.return_value = 384
        e = LocalEmbedder(model_name="all-MiniLM-L6-v2")
        assert e.dimension == 384

    def test_embed_returns_list_of_floats(self, mock_sentence_transformer):
        mock_st, mock_model = mock_sentence_transformer
        mock_model.get_sentence_embedding_dimension.return_value = 384
        mock_model.encode.return_value = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        e = LocalEmbedder(model_name="test-model")
        result = e.embed("hello world")
        assert isinstance(result, list)
        assert len(result) == 3
        assert all(isinstance(v, float) for v in result)

    def test_lazy_loading_loads_model_on_first_call(self, mock_sentence_transformer):
        mock_st, mock_model = mock_sentence_transformer
        mock_model.get_sentence_embedding_dimension.return_value = 384
        mock_model.encode.return_value = np.array([0.1, 0.2], dtype=np.float32)
        e = LocalEmbedder(model_name="test-model")
        mock_st.assert_not_called()
        e.embed("first call")
        mock_st.assert_called_once_with("test-model")

    def test_lazy_loading_only_loads_once(self, mock_sentence_transformer):
        mock_st, mock_model = mock_sentence_transformer
        mock_model.get_sentence_embedding_dimension.return_value = 384
        mock_model.encode.return_value = np.array([0.1, 0.2], dtype=np.float32)
        e = LocalEmbedder(model_name="test-model")
        e.embed("first")
        e.embed("second")
        assert mock_st.call_count == 1

    def test_thread_safety_during_init(self):
        mock_st_cls = MagicMock()
        mock_model = MagicMock()
        mock_model.get_sentence_embedding_dimension.return_value = 384
        mock_model.encode.return_value = np.array([0.1], dtype=np.float32)
        mock_st_cls.return_value = mock_model

        with patch("live_edit.embedder.SentenceTransformer", mock_st_cls):
            e = LocalEmbedder(model_name="test")
            results = []
            errors = []

            def call_embed():
                try:
                    results.append(e.embed("thread"))
                except Exception as ex:
                    errors.append(ex)

            t1 = threading.Thread(target=call_embed)
            t2 = threading.Thread(target=call_embed)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        assert len(results) == 2
        assert len(errors) == 0
        assert mock_st_cls.call_count == 1

    def test_embed_batch_uses_native_batch(self, mock_sentence_transformer):
        mock_st, mock_model = mock_sentence_transformer
        mock_model.get_sentence_embedding_dimension.return_value = 384
        mock_model.encode.return_value = np.array([[0.1], [0.2]], dtype=np.float32)
        e = LocalEmbedder(model_name="test-model")
        results = e.embed_batch(["text1", "text2"])
        assert len(results) == 2
        mock_model.encode.assert_called_once_with(["text1", "text2"])


# numpy is needed for the mock encode return values
import numpy as np
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_embedder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'live_edit.embedder'`

- [ ] **Step 3: Write `live_edit/embedder.py`**

```python
"""Embedder abstract interface and default LocalEmbedder implementation."""

import logging
import threading
from abc import ABC, abstractmethod

logger = logging.getLogger("live-edit.embedder")


class Embedder(ABC):
    """Abstract interface for text-to-vector embedding."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return a float vector for a single text."""

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return float vectors for multiple texts.

        Default loops embed(). Override for optimized batch inference.
        """
        return [self.embed(t) for t in texts]

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Return the embedding vector dimension."""


class LocalEmbedder(Embedder):
    """Default embedder using sentence-transformers (all-MiniLM-L6-v2).

    Lazy-loads the model on first call. Thread-safe initialization.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self._model_name = model_name
        self._model = None
        self._dimension = 0
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)
            self._dimension = self._model.get_sentence_embedding_dimension()
            logger.info("LocalEmbedder loaded model=%s dim=%d",
                        self._model_name, self._dimension)

    def embed(self, text: str) -> list[float]:
        self._ensure_loaded()
        vec = self._model.encode(text)
        return vec.tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._ensure_loaded()
        vecs = self._model.encode(texts)
        return vecs.tolist()

    @property
    def dimension(self) -> int:
        self._ensure_loaded()
        return self._dimension
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `pytest tests/test_embedder.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add live_edit/embedder.py tests/test_embedder.py
git commit -m "feat: add Embedder ABC and LocalEmbedder with lazy loading"
```

---

### Task 3: Storage ABC + SQLite Embedding Methods

**Files:**
- Modify: `live_edit/storage.py`
- Create: tests for new storage methods (in existing `tests/test_storage.py`)

- [ ] **Step 1: Add abstract methods to `Storage` ABC**

Add three new abstract methods to the `Storage` class, after `get_session_detail`:

```python
    @abstractmethod
    def store_embedding(self, session_id: str, request: str,
                        files_json: str, embedding: bytes) -> None:
        """Store a session embedding for later retrieval."""

    @abstractmethod
    def query_embeddings(self) -> list[tuple[str, str, str, bytes]]:
        """Return all stored embeddings as (session_id, request, files_json, embedding_blob)."""

    @abstractmethod
    def delete_old_embeddings(self, keep_count: int) -> None:
        """Delete oldest embeddings, keeping at most `keep_count` most recent rows."""
```

- [ ] **Step 2: Add `session_embeddings` table to `_init_db()`**

In `SQLiteStorage._init_db()`, add after the `live_edit_sessions` table creation:

```python
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL UNIQUE,
                request TEXT NOT NULL,
                files_json TEXT NOT NULL DEFAULT '[]',
                embedding BLOB NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
```

- [ ] **Step 3: Implement `store_embedding()`**

Add to `SQLiteStorage`:

```python
    def store_embedding(self, session_id: str, request: str,
                        files_json: str, embedding: bytes) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO session_embeddings
               (session_id, request, files_json, embedding, created_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (session_id, request, files_json, embedding),
        )
        conn.commit()
```

- [ ] **Step 4: Implement `query_embeddings()`**

Add to `SQLiteStorage`:

```python
    def query_embeddings(self) -> list[tuple[str, str, str, bytes]]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT session_id, request, files_json, embedding
               FROM session_embeddings
               ORDER BY created_at DESC"""
        ).fetchall()
        return [(r[0], r[1], r[2], r[3]) for r in rows]
```

- [ ] **Step 5: Implement `delete_old_embeddings()`**

Add to `SQLiteStorage`:

```python
    def delete_old_embeddings(self, keep_count: int) -> None:
        conn = self._get_conn()
        conn.execute(
            """DELETE FROM session_embeddings
               WHERE id NOT IN (
                   SELECT id FROM session_embeddings
                   ORDER BY created_at DESC LIMIT ?
               )""",
            (keep_count,),
        )
        conn.commit()
```

- [ ] **Step 6: Write tests — add to `tests/test_storage.py`**

Add this class at the end of the file:

```python
class TestSessionEmbeddings:
    @pytest.fixture
    def storage(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        return SQLiteStorage(db_path)

    def test_init_creates_embeddings_table(self, storage):
        conn = storage._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert "session_embeddings" in table_names

    def test_store_and_query_embedding(self, storage):
        emb = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
        storage.store_embedding(
            session_id="s1",
            request="Make button red",
            files_json=json.dumps(["a.py", "b.css"], ensure_ascii=False),
            embedding=emb,
        )
        rows = storage.query_embeddings()
        assert len(rows) == 1
        assert rows[0][0] == "s1"
        assert rows[0][1] == "Make button red"
        assert rows[0][3] == emb

    def test_store_replace_updates_existing(self, storage):
        emb1 = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
        emb2 = struct.pack("4f", 0.5, 0.6, 0.7, 0.8)
        storage.store_embedding("s1", "first", "[]", emb1)
        storage.store_embedding("s1", "updated", '["x.py"]', emb2)
        rows = storage.query_embeddings()
        assert len(rows) == 1
        assert rows[0][1] == "updated"
        assert rows[0][3] == emb2

    def test_query_returns_empty_list_when_no_data(self, storage):
        assert storage.query_embeddings() == []

    def test_delete_old_embeddings_keeps_most_recent(self, storage):
        for i in range(5):
            emb = struct.pack("4f", float(i), 0.0, 0.0, 0.0)
            storage.store_embedding(f"s{i}", f"req{i}", "[]", emb)
            time.sleep(1.1)
        storage.delete_old_embeddings(keep_count=3)
        rows = storage.query_embeddings()
        assert len(rows) == 3
        assert rows[0][1] == "req4"


import struct
```

Also add `import struct` and `import time` at the top of the test file if not already present (check existing imports).

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_storage.py -v -k "TestSessionEmbeddings or TestSQLiteStorage"`
Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add live_edit/storage.py tests/test_storage.py
git commit -m "feat: add embedding CRUD methods to Storage ABC and SQLiteStorage"
```

---

### Task 4: SessionMemory Class

**Files:**
- Create: `live_edit/session_memory.py`
- Create: `tests/test_session_memory.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_memory.py`:

```python
"""Tests for live_edit.session_memory — SessionMemory retrieval and storage."""

import asyncio
import json
import math
import struct
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from live_edit.session_memory import SessionMemory, MemoryEntry
from live_edit.config import SessionMemoryConfig, EmbedderConfig


class FakeEmbedder:
    """Returns fixed vectors for deterministic tests."""
    def __init__(self, dim=4, vectors=None):
        self._dim = dim
        self._vectors = vectors or {}

    def embed(self, text):
        if text in self._vectors:
            return self._vectors[text]
        return [0.0] * self._dim

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]

    @property
    def dimension(self):
        return self._dim


class FakeStorage:
    """In-memory storage for embedding tests."""
    def __init__(self, rows=None):
        self._rows = rows or []

    def store_embedding(self, session_id, request, files_json, embedding):
        self._rows = [r for r in self._rows if r[0] != session_id]
        self._rows.append((session_id, request, files_json, embedding))

    def query_embeddings(self):
        return list(self._rows)

    def delete_old_embeddings(self, keep_count):
        self._rows = self._rows[-keep_count:]


class TestMemoryEntry:
    def test_fields(self):
        entry = MemoryEntry(
            session_id="abc",
            request="Make it blue",
            files={"a.py", "b.py"},
            commit_hash="def123",
            score=0.85,
        )
        assert entry.session_id == "abc"
        assert entry.score == 0.85


class TestSessionMemory:
    @pytest.fixture
    def embedder(self):
        return FakeEmbedder(dim=4, vectors={
            "add login": [1.0, 0.0, 0.0, 0.0],
            "fix button": [0.0, 1.0, 0.0, 0.0],
            "add auth": [0.8, 0.0, 0.0, 0.0],
            "unrelated": [0.0, 0.0, 0.0, 1.0],
        })

    @pytest.fixture
    def storage(self):
        return FakeStorage()

    @pytest.fixture
    def config(self):
        return SessionMemoryConfig(
            enabled=True,
            max_entries=10,
            similarity_threshold=0.5,
            max_stored_entries=100,
        )

    @pytest.fixture
    def session_memory(self, storage, embedder, config):
        return SessionMemory(storage=storage, embedder=embedder, config=config)

    def test_retrieve_empty_storage(self, session_memory):
        results = asyncio.run(session_memory.retrieve("add login"))
        assert results == []

    def test_store_and_retrieve(self, session_memory, storage):
        emb_bytes = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
        # pre-populate storage
        storage.store_embedding("s1", "add login", '["auth.py"]', emb_bytes)

        results = asyncio.run(session_memory.retrieve("add login"))
        assert len(results) == 1
        assert results[0].session_id == "s1"
        assert results[0].request == "add login"
        assert "auth.py" in results[0].files

    def test_identical_request_scores_near_one(self, session_memory, storage):
        emb_bytes = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
        storage.store_embedding("s1", "add login", "[]", emb_bytes)

        results = asyncio.run(session_memory.retrieve("add login"))
        assert len(results) == 1
        assert results[0].score > 0.99

    def test_unrelated_request_scores_low(self, session_memory, storage):
        emb_bytes = struct.pack("4f", 0.0, 0.0, 0.0, 1.0)
        storage.store_embedding("s1", "unrelated", "[]", emb_bytes)

        results = asyncio.run(session_memory.retrieve("add login"))
        assert len(results) == 0  # below threshold

    def test_threshold_filters_low_similarity(self, session_memory, storage):
        # Store an entry whose embedding is orthogonal to the query
        emb_bytes = struct.pack("4f", 0.0, 1.0, 0.0, 0.0)
        storage.store_embedding("s1", "fix button", "[]", emb_bytes)

        results = asyncio.run(session_memory.retrieve("add login"))
        # "add login" query vec = [1,0,0,0], stored vec = [0,1,0,0], cosine=0
        assert results == []

    def test_top_k_truncation(self, session_memory, storage, embedder):
        embedder._dim = 4
        # All entries point in the same direction — all score near 1.0
        for i in range(5):
            emb_bytes = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
            storage.store_embedding(f"s{i}", "add login", "[]", emb_bytes)

        session_memory.config.max_entries = 3
        results = asyncio.run(session_memory.retrieve("add login"))
        assert len(results) == 3

    def test_eviction_on_store(self, session_memory, storage, embedder):
        session_memory.config.max_stored_entries = 3

        async def run():
            for i in range(5):
                await session_memory.store(f"s{i}", f"req{i}", ["a.py"])

        asyncio.run(run())
        rows = storage.query_embeddings()
        assert len(rows) == 3

    def test_deserialize_correct_vector(self, session_memory, storage):
        emb = [0.1, 0.2, 0.3, 0.4]
        emb_bytes = struct.pack("4f", *emb)
        storage.store_embedding("s1", "add login", "[]", emb_bytes)

        results = asyncio.run(session_memory.retrieve("add login"))
        assert len(results) == 1


class TestSessionMemoryDisabled:
    def test_retrieve_skipped_when_disabled(self):
        config = SessionMemoryConfig(enabled=False)
        sm = SessionMemory(
            storage=FakeStorage(),
            embedder=FakeEmbedder(dim=4),
            config=config,
        )
        results = asyncio.run(sm.retrieve("test"))
        assert results == []

    def test_store_skipped_when_disabled(self):
        config = SessionMemoryConfig(enabled=False)
        storage = FakeStorage()
        sm = SessionMemory(storage=storage, embedder=FakeEmbedder(dim=4), config=config)
        asyncio.run(sm.store("s1", "test", []))
        assert storage.query_embeddings() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session_memory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'live_edit.session_memory'`

- [ ] **Step 3: Write `live_edit/session_memory.py`**

```python
"""Session memory — stores and retrieves similar past edit sessions."""

import asyncio
import json
import logging
import math
import struct
from dataclasses import dataclass, field

from .config import SessionMemoryConfig

logger = logging.getLogger("live-edit.session_memory")


@dataclass
class MemoryEntry:
    session_id: str
    request: str
    files: set[str] = field(default_factory=set)
    commit_hash: str = ""
    score: float = 0.0


class SessionMemory:
    """Stores embeddings of past sessions and retrieves similar ones."""

    def __init__(self, storage, embedder, config: SessionMemoryConfig):
        self._storage = storage
        self._embedder = embedder
        self.config = config

    async def store(self, session_id: str, request: str, files: list[str]) -> None:
        """Compute and store embedding for a session after commit."""
        if not self.config.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
            vec = await loop.run_in_executor(None, self._embedder.embed, request)
            emb_bytes = struct.pack(f"{len(vec)}f", *vec)
            files_json = json.dumps(files or [], ensure_ascii=False)
            await loop.run_in_executor(
                None,
                lambda: self._storage.store_embedding(
                    session_id, request, files_json, emb_bytes
                ),
            )
            await self._evict_if_needed()
        except Exception:
            logger.warning("Failed to store embedding for session %s", session_id,
                           exc_info=True)

    async def retrieve(self, request: str) -> list[MemoryEntry]:
        """Find similar past sessions for a new request."""
        if not self.config.enabled:
            return []
        try:
            loop = asyncio.get_running_loop()
            query_vec = await loop.run_in_executor(
                None, self._embedder.embed, request
            )
            rows = await loop.run_in_executor(
                None, self._storage.query_embeddings
            )
            return self._score_and_rank(query_vec, rows)
        except Exception:
            logger.warning("Failed to retrieve session memories", exc_info=True)
            return []

    def _score_and_rank(self, query_vec: list[float],
                        rows: list[tuple]) -> list[MemoryEntry]:
        entries = []
        dim = len(query_vec)
        for session_id, req, files_json, emb_bytes in rows:
            stored_vec = struct.unpack(f"{dim}f", emb_bytes)
            score = self._cosine_similarity(query_vec, stored_vec)
            if score < self.config.similarity_threshold:
                continue
            try:
                files = set(json.loads(files_json))
            except (json.JSONDecodeError, TypeError):
                files = set()
            entries.append(MemoryEntry(
                session_id=session_id,
                request=req,
                files=files,
                score=score,
            ))

        entries.sort(key=lambda e: e.score, reverse=True)
        return entries[:self.config.max_entries]

    @staticmethod
    def _cosine_similarity(a, b) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def _evict_if_needed(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self._storage.delete_old_embeddings(
                self.config.max_stored_entries
            ),
        )
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `pytest tests/test_session_memory.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add live_edit/session_memory.py tests/test_session_memory.py
git commit -m "feat: add SessionMemory class with store, retrieve, and eviction"
```

---

### Task 5: Engine Integration — System Prompt Injection

**Files:**
- Modify: `live_edit/engine.py`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: Add memory injection to `run_edit_session()` — new session path**

In `run_edit_session()`, after the system prompt is built and the worktree is created (after line 459, after preview startup block), add memory retrieval and injection.

Insert after the preview startup block (after line 459):

```python
    # ── Retrieve session memory for context ──
    if (hasattr(config, 'session_memory') and config.session_memory.enabled
            and not continue_msg):
        from .session_memory import SessionMemory
        from .embedder import LocalEmbedder

        sm_embedder = LocalEmbedder(
            model_name=config.session_memory.embedder.model
        )
        session_memory = SessionMemory(
            storage=storage,
            embedder=sm_embedder,
            config=config.session_memory,
        )
        memories = await session_memory.retrieve(session.request)
        if memories:
            memory_context = _format_memory_context(
                memories, config.session_memory.memory_prompt_template
            )
            system_prompt += "\n\n" + memory_context
```

- [ ] **Step 2: Add memory injection to continuation path**

In the `continue_msg and session.messages` branch (around line 461), after repairing and appending the continue message, add:

```python
        # ── Retrieve session memory for continuation ──
        if (hasattr(config, 'session_memory') and config.session_memory.enabled):
            from .session_memory import SessionMemory
            from .embedder import LocalEmbedder

            sm_embedder = LocalEmbedder(
                model_name=config.session_memory.embedder.model
            )
            session_memory = SessionMemory(
                storage=storage,
                embedder=sm_embedder,
                config=config.session_memory,
            )
            memories = await session_memory.retrieve(continue_msg)
            if memories:
                memory_context = _format_memory_context(
                    memories, config.session_memory.memory_prompt_template
                )
                messages.append({"role": "user", "content": memory_context})
```

- [ ] **Step 3: Write `_format_memory_context()` helper**

Add this function before `run_edit_session()`:

```python
def _format_memory_context(memories: list, template: str = "") -> str:
    """Format retrieved MemoryEntry list into a system-prompt-ready string."""
    if template:
        try:
            items = []
            for i, m in enumerate(memories, 1):
                files_str = ", ".join(sorted(m.files)) if m.files else "(none)"
                items.append(
                    template
                    .replace("{index}", str(i))
                    .replace("{request}", m.request)
                    .replace("{files}", files_str)
                    .replace("{commit_hash}", m.commit_hash)
                    .replace("{score}", f"{m.score:.2f}")
                )
            return "\n".join(items)
        except Exception:
            pass

    lines = [
        "## Historical Similar Edit Records",
        "",
        "The following are past requests similar to the current one. Reference them",
        "for patterns and solutions, but adapt to the specific current request.",
        "",
    ]
    for i, m in enumerate(memories, 1):
        files_str = ", ".join(sorted(m.files)) if m.files else "(none)"
        lines.append(f"{i}. Request: \"{m.request}\"")
        lines.append(f"   Files modified: {files_str}")
        if m.commit_hash:
            lines.append(f"   Commit: {m.commit_hash}")
        lines.append(f"   Similarity: {m.score:.2f}")
        lines.append("")

    lines.append("Use the above as reference only — do not blindly copy past solutions.")
    return "\n".join(lines)
```

- [ ] **Step 4: Add memory storage to `_do_commit()`**

In `_do_commit()`, after `session._commit_hash = wt_hash` (line 413) and before `session._committed = True` (line 414), add:

```python
        # ── Store session embedding for future retrieval ──
        if config and hasattr(config, 'session_memory') and config.session_memory.enabled:
            try:
                from .session_memory import SessionMemory
                from .embedder import LocalEmbedder
                sm_embedder = LocalEmbedder(
                    model_name=config.session_memory.embedder.model
                )
                session_memory = SessionMemory(
                    storage=storage,
                    embedder=sm_embedder,
                    config=config.session_memory,
                )
                await session_memory.store(
                    session_id=session.id,
                    request=session.request,
                    files=session._modified_files,
                )
            except Exception as e:
                logger.warning("Failed to store session memory: %s", e)
```

- [ ] **Step 5: Write engine integration tests — add to `tests/test_engine.py`**

Add this class at the end of the test file:

```python
class TestFormatMemoryContext:
    def test_default_template(self):
        from live_edit.session_memory import MemoryEntry
        from live_edit.engine import _format_memory_context

        memories = [
            MemoryEntry(session_id="s1", request="Fix auth",
                        files={"auth.py"}, commit_hash="abc", score=0.95),
        ]
        result = _format_memory_context(memories)
        assert "Historical Similar Edit Records" in result
        assert "Fix auth" in result
        assert "auth.py" in result
        assert "0.95" in result

    def test_custom_template(self):
        from live_edit.session_memory import MemoryEntry
        from live_edit.engine import _format_memory_context

        memories = [
            MemoryEntry(session_id="s1", request="Fix auth",
                        files={"auth.py"}, commit_hash="abc", score=0.95),
        ]
        template = "[{index}] {request} ({files}) score={score}"
        result = _format_memory_context(memories, template)
        assert "[1] Fix auth (auth.py) score=0.95" in result

    def test_empty_memories(self):
        from live_edit.engine import _format_memory_context
        result = _format_memory_context([])
        assert "Historical" in result


class TestSessionMemoryEngineIntegration:
    """Integration tests for session memory in the engine."""

    @pytest.mark.asyncio
    async def test_session_memory_disabled_by_default(self):
        """When session_memory is disabled, no memory injection or errors."""
        from live_edit.engine import run_edit_session, EditSession

        config = Config()
        session = EditSession("test-s1", "Make it red")
        mock_provider = AsyncMock()
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = "/tmp/test-s1"
        mock_vcs.commit_in_worktree.return_value = "fakehash"
        mock_storage = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_tools.return_value = []

        mock_provider.call_with_tools.return_value = [
            {"type": "text", "text": "I'll make it red."},
        ]

        await run_edit_session(
            session=session, provider=mock_provider, vcs=mock_vcs,
            storage=mock_storage, config=config, mode="deep",
            tool_registry=mock_registry,
        )
        # Should complete without errors
        assert True
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_engine.py -v -k "TestFormatMemoryContext or TestSessionMemoryEngineIntegration"`
Expected: all PASS

Also verify existing engine tests still pass:
Run: `pytest tests/test_engine.py -v -k "TestTranslateError or TestBuildTimeline or TestEditSession or TestSessionStore"`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add live_edit/engine.py tests/test_engine.py
git commit -m "feat: integrate session memory into engine — inject on start, store on commit"
```

---

### Task 6: Dependency & Integration Finalization

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `[rag]` optional dependency**

In `pyproject.toml`, add to `[project.optional-dependencies]`:

```toml
rag = ["sentence-transformers>=3.0"]
```

- [ ] **Step 2: Verify the dependency installs**

Run: `pip install -e ".[rag]" 2>&1 | tail -5`
Expected: installs sentence-transformers and its dependencies

- [ ] **Step 3: Run all existing tests to verify no regressions**

Run: `pytest tests/ -v --ignore=tests/test_embedder.py --ignore=tests/test_session_memory.py 2>&1 | tail -30`
Expected: all PASS

- [ ] **Step 4: Run all new tests**

Run: `pytest tests/test_embedder.py tests/test_session_memory.py tests/test_storage.py tests/test_engine.py -v 2>&1 | tail -30`
Expected: all PASS

- [ ] **Step 5: Full test suite**

Run: `pytest tests/ -v 2>&1 | tail -40`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml
git commit -m "feat: add sentence-transformers optional dependency for RAG session memory"
```

---

### Task 7: Startup Validation & ImportError Handling

**Files:**
- Modify: `live_edit/embedder.py`
- Modify: `live_edit/engine.py`

- [ ] **Step 1: Add startup validation to `run_edit_session()`**

In `run_edit_session()`, replace the inline `LocalEmbedder` + `SessionMemory` instantiation in both the new-session and continuation paths with a shared factory. Extract into a helper at the top of `run_edit_session()`:

Replace both inline blocks with this pattern (put once before the memory retrieval check):

```python
    # ── Session memory setup (validated once at startup) ──
    from .session_memory import SessionMemory
    from .embedder import LocalEmbedder

    session_memory = None
    if hasattr(config, 'session_memory') and config.session_memory.enabled:
        try:
            sm_embedder = LocalEmbedder(
                model_name=config.session_memory.embedder.model
            )
            # Validate embedding works before using in session
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, sm_embedder.embed, "test")
            session_memory = SessionMemory(
                storage=storage,
                embedder=sm_embedder,
                config=config.session_memory,
            )
        except ImportError:
            logger.warning(
                "session_memory.enabled=true but sentence-transformers is not "
                "installed. Install with: pip install live-edit[rag]"
            )
        except Exception as e:
            logger.warning(
                "Session memory disabled: embedding model failed to load: %s", e
            )
```

Then in the new-session path:
```python
    if session_memory is not None and not continue_msg:
        memories = await session_memory.retrieve(session.request)
        ...
```

And in the continuation path:
```python
    if session_memory is not None:
        memories = await session_memory.retrieve(continue_msg)
        ...
```

And in `_do_commit()`, replace the inline block with a simpler check — pass `session_memory` in or do the same validation:

Since `_do_commit` can't access the `session_memory` variable from `run_edit_session()`, store it on the session object:

In `run_edit_session()`, after creating `session_memory`:
```python
    session._session_memory = session_memory
```

In `_do_commit()`, replace the inline embedder instantiation:
```python
        sm = getattr(session, '_session_memory', None)
        if sm is not None:
            try:
                await sm.store(
                    session_id=session.id,
                    request=session.request,
                    files=session._modified_files,
                )
            except Exception as e:
                logger.warning("Failed to store session memory: %s", e)
```

- [ ] **Step 2: Write test for the ImportError case**

Add to `tests/test_engine.py`:

```python
    @pytest.mark.asyncio
    async def test_missing_rag_dependency_logs_warning(self):
        """When rag dep is missing, engine should warn but not crash."""
        from live_edit.engine import run_edit_session, EditSession

        config = Config()
        config.session_memory.enabled = True

        session = EditSession("test-s2", "Make it red")
        mock_provider = AsyncMock()
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = "/tmp/test-s2"
        mock_vcs.commit_in_worktree.return_value = "fakehash"
        mock_storage = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_tools.return_value = []

        mock_provider.call_with_tools.return_value = [
            {"type": "text", "text": "Done."},
        ]

        with patch("live_edit.engine.LocalEmbedder", side_effect=ImportError("No module")):
            await run_edit_session(
                session=session, provider=mock_provider, vcs=mock_vcs,
                storage=mock_storage, config=config, mode="deep",
                tool_registry=mock_registry,
            )
        # Should complete without raising
```

Add `from unittest.mock import patch` import check near the top of the test file.

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_engine.py -v -k "TestSessionMemoryEngineIntegration" 2>&1`
Expected: all PASS

Run: `pytest tests/ -v 2>&1 | tail -20`
Expected: all PASS, no regressions

- [ ] **Step 4: Commit**

```bash
git add live_edit/embedder.py live_edit/engine.py tests/test_engine.py
git commit -m "feat: add startup validation and graceful degradation for session memory"
```
