import hashlib
import json
import math  # noqa: F401  (kept verbatim from brief)
import struct
import time  # noqa: F401  (kept verbatim from brief)
from unittest.mock import MagicMock, patch  # noqa: F401  (kept verbatim from brief)

import pytest  # noqa: F401  (kept verbatim from brief)

from live_edit.config import LongTermConfig
from live_edit.memory import (  # noqa: F401  (MemoryEntry kept verbatim from brief)
    LongTermMemory,
    MemoryEntry,
)


class FakeEmbedder:
    """Returns simple deterministic embeddings for testing."""

    def __init__(self, dim=384):
        self._dim = dim

    def embed(self, text: str) -> list[float]:
        # Deterministic and always non-zero: constant vectors in [0.5, 1.0),
        # so cosine similarity between any two is exactly 1.0 (no zero-vector flake).
        h = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)
        v = 0.5 + (h % 1000) / 2000.0
        return [v] * self._dim

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
        return [
            (
                c["id"],
                c["session_id"],
                c["commit_hash"],
                c["chunk_type"],
                c["chunk_text"],
                c["payload_json"],
                c.get("file_path", ""),
                c["embedding_bytes"],
                c.get("hit_count", 0),
                c.get("last_accessed", None),
            )
            for c in self.chunks[-limit:]
        ]

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
            self.chunks.append(
                {
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
                }
            )
            self._next_id += 1

    def delete_old_sessions(self, keep_count: int) -> None:
        sessions = list(
            dict.fromkeys(c["session_id"] for c in sorted(self.chunks, key=lambda c: c["id"]))
        )
        if len(sessions) > keep_count:
            to_delete = set(sessions[:-keep_count])
            self.chunks = [c for c in self.chunks if c["session_id"] not in to_delete]


class TestLongTermMemory:
    def test_retrieve_returns_similar_chunks(self):
        cfg = LongTermConfig(
            enabled=True,
            max_entries=5,
            similarity_threshold=0.0,  # accept everything for test
            recency_decay_rate=0.0,  # no decay
            hit_count_weight=0.0,  # no hit bonus
        )
        storage = FakeStorage()
        embedder = FakeEmbedder(dim=384)
        ltm = LongTermMemory(storage, embedder, cfg)

        # Store some chunks
        emb = struct.pack("384f", *[0.5] * 384)
        storage.store_chunks(
            "s1",
            "abc",
            [
                {
                    "chunk_type": "request",
                    "chunk_text": "fix login bug",
                    "payload_json": json.dumps({"request": "fix login bug"}),
                    "file_path": "",
                    "embedding_bytes": emb,
                },
            ],
        )
        storage.store_chunks(
            "s2",
            "def",
            [
                {
                    "chunk_type": "request",
                    "chunk_text": "add navbar",
                    "payload_json": json.dumps({"request": "add navbar"}),
                    "file_path": "",
                    "embedding_bytes": emb,
                },
            ],
        )

        results = ltm.retrieve_sync("fix auth bug")
        assert len(results) > 0

    def test_retrieve_skips_malformed_embedding_row(self):
        """A malformed embedding BLOB (wrong dimension) must not zero the whole query."""
        cfg = LongTermConfig(
            enabled=True,
            max_entries=5,
            similarity_threshold=0.0,
            recency_decay_rate=0.0,
            hit_count_weight=0.0,
        )
        storage = FakeStorage()
        embedder = FakeEmbedder(dim=384)
        ltm = LongTermMemory(storage, embedder, cfg)

        good_emb = struct.pack("384f", *[0.5] * 384)
        bad_emb = struct.pack("16f", *[0.5] * 16)  # wrong length for a dim-384 unpack

        storage.store_chunks(
            "s-good",
            "hash1",
            [
                {
                    "chunk_type": "request",
                    "chunk_text": "fix login bug",
                    "payload_json": json.dumps({"request": "fix login bug"}),
                    "file_path": "",
                    "embedding_bytes": good_emb,
                },
            ],
        )
        storage.store_chunks(
            "s-bad",
            "hash2",
            [
                {
                    "chunk_type": "request",
                    "chunk_text": "malformed row",
                    "payload_json": json.dumps({"request": "malformed row"}),
                    "file_path": "",
                    "embedding_bytes": bad_emb,
                },
            ],
        )

        results = ltm.retrieve_sync("fix login bug")
        assert len(results) >= 1
        assert results[0].request == "fix login bug"
        assert all(r.session_id == "s-good" for r in results)

    def test_store_creates_chunks(self):
        cfg = LongTermConfig(enabled=True)
        storage = FakeStorage()
        embedder = FakeEmbedder(dim=384)
        ltm = LongTermMemory(storage, embedder, cfg)

        import asyncio

        asyncio.run(
            ltm.store(
                "s1",
                "update README",
                ["README.md"],
                "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n",
                "abc123",
            )
        )

        assert len(storage.chunks) > 0
        assert any(c["chunk_type"] == "request" for c in storage.chunks)
        assert any(c["chunk_type"] == "file_diff" for c in storage.chunks)

    def test_recency_decay_reduces_old_scores(self):
        cfg = LongTermConfig(
            enabled=True,
            similarity_threshold=0.0,
            recency_decay_rate=1.0,  # strong decay
            hit_count_weight=0.0,
            max_entries=5,
        )
        storage = FakeStorage()
        embedder = FakeEmbedder(dim=384)
        ltm = LongTermMemory(storage, embedder, cfg)

        emb = struct.pack("384f", *[0.9] * 384)
        storage.store_chunks(
            "old_session",
            "oldhash",
            [
                {
                    "chunk_type": "request",
                    "chunk_text": "old edit",
                    "payload_json": json.dumps({"request": "old edit"}),
                    "file_path": "",
                    "embedding_bytes": emb,
                },
            ],
        )
        # Set last_accessed to 100 days ago
        storage.chunks[0]["last_accessed"] = "2026-04-25T00:00:00"

        results = ltm.retrieve_sync("old edit")
        # Score should be heavily decayed by exp(-1.0 * 100) ≈ 0
        assert len(results) > 0
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
        storage.store_chunks(
            "s1",
            "abc",
            [
                {
                    "chunk_type": "request",
                    "chunk_text": "popular edit",
                    "payload_json": json.dumps({"request": "popular edit"}),
                    "file_path": "",
                    "embedding_bytes": emb,
                },
            ],
        )
        storage.chunks[0]["hit_count"] = 10  # max bonus

        results = ltm.retrieve_sync("popular edit")
        # Score should include hit bonus: base ~1.0 + 0.05*min(10,10) = 1.5
        assert len(results) > 0
        assert results[0].score > 1.0  # fails if hit bonus were removed (baseline 1.0)

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
        storage.store_chunks(
            "s1",
            "abc",
            [
                {
                    "chunk_type": "request",
                    "chunk_text": "viral edit",
                    "payload_json": json.dumps({"request": "viral edit"}),
                    "file_path": "",
                    "embedding_bytes": emb,
                },
            ],
        )
        storage.chunks[0]["hit_count"] = 50  # way over cap

        results = ltm.retrieve_sync("viral edit")
        # Bonus should be 0.05 * 10 = 0.5, not 0.05 * 50 = 2.5
        assert len(results) > 0
        assert results[0].score <= 1.0 + 0.5 + 0.05  # within bounds
