# tests/test_memory_manager.py
import json
import struct
from unittest.mock import MagicMock, patch  # noqa: F401  (kept verbatim from brief)

import pytest

from live_edit.config import KnowledgeConfig, LongTermConfig, MemoryConfig, ShortTermConfig
from live_edit.memory import (  # noqa: F401  (kept verbatim from brief)
    KnowledgeEntry,
    MemoryEntry,
    MemoryManager,
)


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
            self.chunks.append(
                {
                    "id": self._next_id,
                    "session_id": session_id,
                    "commit_hash": commit_hash,
                    **ch,
                    "hit_count": 0,
                    "last_accessed": None,
                }
            )
            self._next_id += 1

    def delete_old_sessions(self, keep_count):
        pass

    def query_knowledge_chunks(self, limit=15000):
        return [
            (
                c["id"],
                c["source_path"],
                c["chunk_index"],
                c["chunk_text"],
                c["metadata_json"],
                c["embedding_bytes"],
                c.get("hit_count", 0),
                c.get("last_accessed", None),
            )
            for c in self.knowledge_chunks
        ]

    def query_knowledge_chunks_vec(self, query_emb, limit):
        return self.query_knowledge_chunks(limit)

    def list_knowledge_meta(self):
        return self.knowledge_meta


class TestMemoryManager:
    @pytest.fixture
    def mgr(self):
        cfg = MemoryConfig(
            enabled=True,
            short_term=ShortTermConfig(
                max_full_rounds=3, max_stripped_rounds=7, max_summary_rounds=20
            ),
            long_term=LongTermConfig(
                enabled=True,
                similarity_threshold=0.0,
                recency_decay_rate=0.0,
                hit_count_weight=0.0,
                max_entries=5,
            ),
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
        mgr._storage.store_chunks(
            "past_sess",
            "hash1",
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

        msgs = [{"role": "user", "content": "fix auth"}]
        context, _ = mgr.retrieve_sync("fix auth", "s1", msgs, round_num=1)
        assert "Relevant Past Changes" in context or len(context) > 0

    def test_store_delegates_to_l2(self, mgr):
        mgr.store_sync("s1", "update readme", ["README.md"], "diff --git a/README.md ...", "abc123")
        assert len(mgr._storage.chunks) > 0

    def test_disabled_master_switch_skips_all(self):
        cfg = MemoryConfig(enabled=False)
        mgr = MemoryManager(FakeStorage(), FakeEmbedder(dim=384), cfg)
        msgs = [{"role": "user", "content": "test"}]
        context, _ = mgr.retrieve_sync("test", "s1", msgs, round_num=10)
        assert "Relevant Past Changes" not in context
