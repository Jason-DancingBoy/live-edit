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
        # Use vector close to query [1,0,0,0] so cosine similarity > 0.5 threshold
        emb = [0.9, 0.0, 0.0, 0.0]
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
