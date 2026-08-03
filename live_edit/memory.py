"""Three-tier memory system: ShortTermMemory, LongTermMemory, KnowledgeBase, MemoryManager."""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import struct
from dataclasses import dataclass

from .config import KnowledgeConfig, LongTermConfig, MemoryConfig, ShortTermConfig

logger = logging.getLogger("live-edit.memory")


@dataclass
class MemoryEntry:
    session_id: str
    request: str
    file_path: str = ""
    diff_summary: str = ""
    stat: str = ""
    commit_hash: str = ""
    score: float = 0.0


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

    def manage(self, messages: list[dict], round_num: int) -> tuple[list[dict], str]:
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

    def _strip_old_rounds(self, messages: list[dict], keep_full: int) -> list[dict]:
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
        result.extend(messages[-keep_msgs:] if keep_msgs else [])
        return result

    async def _summarize(self, messages: list[dict], keep_full: int, provider) -> str:
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

        summary_model = self.config.summary_model or ""  # noqa: F841  (kept verbatim from spec)
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
                return "[会话摘要] " + str(block.get("text", "")).strip()
        return ""


class LongTermMemory:
    """L2: Long-term memory over chunked past sessions.

    Stores 1 request chunk + 1 file_diff chunk per modified file, then retrieves
    similar chunks scored by cosine similarity, recency decay, and hit-count bonus.
    """

    def __init__(self, storage, embedder, config: LongTermConfig):
        self._storage = storage
        self._embedder = embedder
        self.config = config
        self._migrate_if_needed()
        # Rebuild vec tables if the embedder dimension changed since last init.
        if hasattr(self._storage, "_ensure_vec_dimension"):
            try:
                self._storage._ensure_vec_dimension(self._embedder.dimension)
            except Exception:
                logger.warning("vec dimension check failed; using brute-force fallback")

    def _migrate_if_needed(self) -> None:
        """Delegate schema migration to storage, then copy any legacy v1 data.

        The storage auto-migrates a fresh DB to schema v2 (hit_count /
        last_accessed + vec tables). DBs created before v2 also have a v1
        `session_embeddings` table whose rows must be copied into
        `session_chunks` so existing session memory survives the upgrade.
        """
        if not hasattr(self._storage, "_migrate_to_memory_v2"):
            return  # FakeStorage in tests
        try:
            self._storage._migrate_to_memory_v2()
        except Exception:
            logger.warning(
                "L2 storage migration failed; continuing with brute-force fallback",
                exc_info=True,
            )
        self._migrate_v1_session_embeddings()

    def _migrate_v1_session_embeddings(self) -> None:
        """Copy legacy v1 `session_embeddings` rows into `session_chunks`.

        INSERT OR IGNORE with a temp unique index on (session_id, chunk_type)
        guarantees crash-safe re-runs. Falls back silently if the old table is
        missing or sqlite-vec is unavailable.
        """
        try:
            conn = self._storage._get_conn()
        except AttributeError:
            return  # FakeStorage in tests -- skip

        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_embeddings'"
        ).fetchone()
        if not exists:
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
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM session_chunks").fetchone()[0]
            logger.info("Migration complete: %d chunks in session_chunks", count)
        except Exception:
            conn.execute("DROP INDEX IF EXISTS idx_chunks_migration")
            conn.rollback()
            logger.warning(
                "Migration from session_embeddings failed; "
                "session memory will start with empty chunks.",
                exc_info=True,
            )

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

    async def retrieve(self, query: str) -> list[MemoryEntry]:
        """Find similar past chunks, grouped by session (async)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.retrieve_sync, query)

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
        """Score chunks with cosine + recency decay + hit bonus, group by session, rank."""
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
                    accessed_dt = datetime.fromisoformat(last_accessed.replace("Z", "+00:00"))
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

            scored.append(
                (
                    session_id,
                    chunk_type,
                    file_path,
                    final_score,
                    payload.get("request", ""),
                    payload.get("stat", ""),
                    commit_hash,
                    payload.get("diff", ""),
                )
            )
            matched_ids.append(chunk_id)

        # Update hit counts for matched chunks
        if matched_ids:
            try:  # noqa: SIM105  (brief-mandated except Exception: pass)
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
                entries.append(
                    MemoryEntry(
                        session_id=_sid,
                        request=req,
                        file_path=fpath,
                        diff_summary="\n".join(diff_lines),
                        stat=stat,
                        commit_hash=chash,
                        score=score,
                    )
                )

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
                        with open(fpath, encoding="utf-8") as f:
                            content = f.read()
                        disk_files[fname] = content
                    except Exception as e:
                        logger.warning("Failed to read knowledge file %s: %s", fpath, e)

        # Collect existing meta
        existing_meta = {
            m["source_path"]: m
            for m in self._storage.list_knowledge_meta()
            if m["source_type"] == "file"
        }

        # Find added/updated
        for fname, content in disk_files.items():
            file_hash = hashlib.sha256(content.encode()).hexdigest()
            try:
                if fname not in existing_meta:
                    self._index_document(fname, content, "file", file_hash)
                    result["added"] += 1
                elif existing_meta[fname].get("file_hash") != file_hash:
                    self._index_document(fname, content, "file", file_hash)
                    result["updated"] += 1
            except Exception as e:
                logger.warning("Failed to index knowledge file %s: %s", fname, e)
                continue

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
        for i, (text, vec) in enumerate(zip(chunks_text, embeddings, strict=True)):
            chunk_dicts.append(
                {
                    "source_path": source_path,
                    "chunk_index": i,
                    "chunk_text": text,
                    "embedding_bytes": struct.pack(f"{dim}f", *vec),
                    "metadata_json": json.dumps({}, ensure_ascii=False),
                }
            )

        self._storage.store_knowledge_chunks(source_path, chunk_dicts)
        self._storage.upsert_knowledge_meta(source_path, source_type, file_hash, len(chunk_dicts))

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
                        chunks.append(para[i : i + chunk_size])
                    current = ""  # reset so the stale buffer is not emitted again
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
                entries.append(
                    KnowledgeEntry(
                        source_path=source_path,
                        chunk_text=chunk_text,
                        score=score,
                    )
                )

            entries.sort(key=lambda e: e.score, reverse=True)
            return entries[: self.config.max_entries]
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
        return dot / (norm_a * norm_b)  # type: ignore[no-any-return]

    # --- API Document Management ---

    def add_api_document(self, source_path: str, content: str, metadata: dict) -> None:
        """Add or update an API-uploaded document."""
        if not source_path.startswith("api:"):
            raise ValueError("API document source_path must start with 'api:'")
        self._index_document(
            source_path,
            content,
            "api",
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
        return self._storage.list_knowledge_meta()  # type: ignore[no-any-return]


class MemoryManager:
    """Unified three-tier memory. Engine calls this single entry point."""

    def __init__(self, storage, embedder, config: MemoryConfig, provider=None):
        self.config = config
        self._storage = storage
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

    def manage_messages(self, messages: list[dict], round_num: int) -> tuple[list[dict], str]:
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
                parts.append(
                    _format_memory_context(memories, self.config.long_term.memory_prompt_template)
                )
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
                parts.append(
                    _format_memory_context(memories, self.config.long_term.memory_prompt_template)
                )
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
        self,
        session_id: str,
        request: str,
        files: list[str],
        diff: str,
        commit_hash: str,
    ) -> None:
        """Store session in L2 long-term memory."""
        if self._long_term is not None:
            await self._long_term.store(session_id, request, files, diff, commit_hash)

    def store_sync(
        self, session_id: str, request: str, files: list[str], diff: str, commit_hash: str
    ) -> None:
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
        lines.append(f'- {fname}: "{text}..."')
    return "\n".join(lines)
