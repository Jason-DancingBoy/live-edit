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
