"""Session memory --- stores and retrieves similar past edit sessions."""

import asyncio
import json
import logging
import math
import re
import struct
from dataclasses import dataclass

from .config import SessionMemoryConfig

logger = logging.getLogger("live-edit.session_memory")


@dataclass
class MemoryEntry:
    session_id: str
    request: str
    file_path: str = ""
    diff_summary: str = ""
    stat: str = ""
    commit_hash: str = ""
    score: float = 0.0


class SessionMemory:
    """Stores chunked embeddings of past sessions and retrieves similar ones."""

    def __init__(self, storage, embedder, config: SessionMemoryConfig):
        self._storage = storage
        self._embedder = embedder
        self.config = config
        self._migrate_if_needed()

    def _migrate_if_needed(self) -> None:
        """Idempotently migrate v1 session_embeddings to session_chunks.

        Uses PRAGMA user_version as migration gate:
          0 = not migrated (or fresh DB), 1 = migration complete.
        SQLiteStorage now auto-migrates a fresh DB to schema v2 (hit_count /
        last_accessed + vec tables); the v1 session_embeddings migration must
        still run in that case, but it must never downgrade user_version below 2.

        INSERT OR IGNORE with a temp unique index on (session_id, chunk_type)
        guarantees crash-safe re-runs.
        """
        try:
            conn = self._storage._get_conn()
        except AttributeError:
            return  # FakeStorage in tests -- skip

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            return  # v1 migration already complete

        # Check old table exists
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_embeddings'"
        ).fetchone()
        if not exists:
            if version < 2:
                conn.execute("PRAGMA user_version = 1")
            conn.commit()
            return

        logger.info("Migrating v1 session_embeddings to session_chunks...")
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_migration "
                "ON session_chunks(session_id, chunk_type)"
            )
            conn.execute(
                """INSERT OR IGNORE INTO session_chunks
                   (session_id, chunk_type, chunk_text, payload_json,
                    embedding, created_at)
                   SELECT
                       session_id,
                       'request',
                       request,
                       json_object(
                           'request', request,
                           'files', files_json,
                           'migrated', json('true')
                       ),
                       embedding,
                       created_at
                   FROM session_embeddings"""
            )
            conn.execute("DROP INDEX IF EXISTS idx_chunks_migration")
            # Sync the vec table so migrated chunks are searchable (idempotent:
            # only rows absent from vec are inserted). Falls back to brute-force
            # if sqlite-vec is unavailable.
            try:
                conn.execute("""
                    INSERT INTO session_chunks_vec (rowid, embedding)
                    SELECT id, embedding FROM session_chunks
                    WHERE id NOT IN (SELECT rowid FROM session_chunks_vec)
                """)
                conn.commit()
            except Exception:
                logger.warning(
                    "Could not sync session_chunks_vec after v1 migration; "
                    "brute-force fallback will be used"
                )
            if version < 2:
                conn.execute("PRAGMA user_version = 1")
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM session_chunks").fetchone()[0]
            logger.info("Migration complete: %d chunks in session_chunks", count)
        except Exception:
            conn.execute("DROP INDEX IF EXISTS idx_chunks_migration")
            conn.rollback()
            logger.warning(
                "Migration from session_embeddings failed; "
                "session memory will start with empty chunks. "
                "To retry migration, run: PRAGMA user_version = 0",
                exc_info=True,
            )
            if version < 2:
                conn.execute("PRAGMA user_version = 1")
            conn.commit()

    # --- Public API ---

    async def store(
        self, session_id: str, request: str, files: list[str], diff: str, commit_hash: str
    ) -> None:
        """Chunk session by file and store embeddings transactionally.

        Produces 1 request chunk + 1 file_diff chunk per modified file.
        Old chunks for this session_id are replaced atomically.
        """
        if not self.config.enabled:
            return
        try:
            loop = asyncio.get_running_loop()

            # Parse diff into per-file chunks
            file_chunks = await loop.run_in_executor(None, self._split_diff_by_file, diff)

            # Build all chunk_texts for batch embedding
            chunk_texts = [request]  # request chunk
            for fc in file_chunks:
                chunk_texts.append(f"{request}\nFile: {fc['file_path']}\nChanges: {fc['stat']}")

            # Batch embed (CPU-bound)
            embeddings = await loop.run_in_executor(None, self._embedder.embed_batch, chunk_texts)

            # Construct chunk dicts with embeddings
            dim = len(embeddings[0])
            chunks = []

            # Request chunk
            chunks.append(
                {
                    "chunk_type": "request",
                    "chunk_text": chunk_texts[0],
                    "payload_json": json.dumps(
                        {
                            "request": request,
                            "files": files or [],
                            "commit_hash": commit_hash,
                        },
                        ensure_ascii=False,
                    ),
                    "file_path": "",
                    "embedding_bytes": struct.pack(f"{dim}f", *embeddings[0]),
                }
            )

            # File-diff chunks
            for i, fc in enumerate(file_chunks):
                payload = {
                    "file": fc["file_path"],
                    "diff": fc["diff_content"][:3000],
                    "stat": fc["stat"],
                    "request": request,
                    "commit_hash": commit_hash,
                }
                chunks.append(
                    {
                        "chunk_type": "file_diff",
                        "chunk_text": chunk_texts[i + 1],
                        "payload_json": json.dumps(payload, ensure_ascii=False),
                        "file_path": fc["file_path"],
                        "embedding_bytes": struct.pack(f"{dim}f", *embeddings[i + 1]),
                    }
                )

            # Transactional write + eviction in one run_in_executor call
            max_sessions = self.config.max_stored_entries
            await loop.run_in_executor(
                None,
                lambda: self._storage.store_chunks(session_id, commit_hash, chunks),
            )
            # Evict old sessions (fire-and-forget; session-level)
            await loop.run_in_executor(
                None,
                lambda: self._storage.delete_old_sessions(max_sessions),
            )

        except Exception:
            logger.warning(
                "Failed to store chunks for session %s",
                session_id,
                exc_info=True,
            )

    async def retrieve(self, request: str) -> list[MemoryEntry]:
        """Find similar past chunks, grouped by session."""
        if not self.config.enabled:
            return []
        try:
            loop = asyncio.get_running_loop()
            query_vec = await loop.run_in_executor(None, self._embedder.embed, request)
            rows = await loop.run_in_executor(None, self._storage.query_chunks)
            return self._score_and_rank(query_vec, rows)
        except Exception:
            logger.warning("Failed to retrieve session memories", exc_info=True)
            return []

    # --- Diff parsing ---

    @staticmethod
    def _split_diff_by_file(diff: str) -> list[dict]:
        """Split a unified diff into per-file entries.

        Returns list of {file_path, stat, diff_content}.
        Skips binary files and empty diffs.
        """
        if not diff or not diff.strip():
            return []

        # Split on 'diff --git ' boundaries
        parts = re.split(r"^(?=diff --git )", diff, flags=re.MULTILINE)
        results = []

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Check for binary
            if re.search(r"^Binary files ", part, re.MULTILINE):
                continue

            # Extract file path from ---/+++ headers
            file_match = re.search(r"^\+\+\+ b/(.+)$", part, re.MULTILINE)
            if not file_match:
                # Try rename
                rename_match = re.search(r"^rename (?:from|to) (.+)$", part, re.MULTILINE)
                if rename_match:
                    file_path = rename_match.group(1)
                else:
                    continue
            else:
                file_path = file_match.group(1)

            # Count stat
            lines_added = len(re.findall(r"^\+(?!\+\+)", part, re.MULTILINE))
            lines_removed = len(re.findall(r"^-(?!--)", part, re.MULTILINE))
            stat = f"+{lines_added}/-{lines_removed}"

            results.append(
                {
                    "file_path": file_path,
                    "stat": stat,
                    "diff_content": part,
                }
            )

        return results

    # --- Scoring ---

    def _score_and_rank(self, query_vec: list[float], rows: list[tuple]) -> list[MemoryEntry]:
        """Score chunks, group by session (top-2, prefer file_diff), rank."""
        dim = len(query_vec)
        scored = []  # (session_id, chunk_type, file_path, score, payload)

        for row in rows:
            # row = (id, session_id, commit_hash, chunk_type,
            #        chunk_text, payload_json, file_path, embedding)
            session_id = row[1]
            commit_hash = row[2]
            chunk_type = row[3]
            file_path = row[6] or ""
            emb_bytes = row[7]

            stored_vec = struct.unpack(f"{dim}f", emb_bytes)
            score = self._cosine_similarity(query_vec, stored_vec)
            if score < self.config.similarity_threshold:
                continue

            try:
                payload = json.loads(row[5])
            except (json.JSONDecodeError, TypeError):
                payload = {}

            scored.append(
                (
                    session_id,
                    chunk_type,
                    file_path,
                    score,
                    payload.get("request", ""),
                    payload.get("stat", ""),
                    commit_hash,
                    payload.get("diff", ""),
                )
            )

        # Group by session_id
        by_session: dict[str, list] = {}
        for item in scored:
            sid = item[0]
            if sid not in by_session:
                by_session[sid] = []
            by_session[sid].append(item)

        # Per session: pick top-2, prefer file_diff
        entries = []
        for _sid, items in by_session.items():
            # Sort: file_diff first (bonus), then by score desc
            def _sort_key(item):
                chunk_type = item[1]
                score = item[3]
                type_bonus = 0.0 if chunk_type == "file_diff" else -0.05
                return score + type_bonus

            items.sort(key=_sort_key, reverse=True)
            picked = items[:2]

            for _sid, _ct, fpath, score, req, stat, chash, diff in picked:
                # diff_summary: first 4 non-empty lines of diff, ~120 chars
                diff_lines = [line for line in (diff or "").split("\n") if line.strip()][:4]
                diff_summary = "\n".join(diff_lines)

                entries.append(
                    MemoryEntry(
                        session_id=_sid,
                        request=req,
                        file_path=fpath,
                        diff_summary=diff_summary,
                        stat=stat,
                        commit_hash=chash,
                        score=score,
                    )
                )

        # Sort across sessions by best chunk score, take top max_entries
        entries.sort(key=lambda e: e.score, reverse=True)
        return entries[: self.config.max_entries]

    @staticmethod
    def _cosine_similarity(a, b) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)  # type: ignore[no-any-return]
