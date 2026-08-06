"""Functional tests for cross-session / cross-user L2 long-term memory.

Validates the contract that a new session (even from a different user) can
retrieve semantically similar past edits from the shared project DB, plus
scoring, store, formatting, eviction, fallback, and migration behavior.
See docs/superpowers/specs/2026-08-06-cross-session-memory-functional-tests-design.md
"""

import asyncio
import hashlib
import json
import re
import struct

import pytest

from live_edit.config import (
    KnowledgeConfig,
    LongTermConfig,
    MemoryConfig,
    ShortTermConfig,
)
from live_edit.memory import LongTermMemory, MemoryManager


class TopicFakeEmbedder:
    """Deterministic, semantically discriminative embedder.

    Each topic maps to an orthogonal basis vector; embed() classifies text by
    keyword (longest-match-first) into a topic and adds a tiny hash-seeded
    perturbation. Same-topic texts have cosine > 0.95 (delta < 0.05), so the
    -0.05 tiebreak in LongTermMemory._score_and_rank is decisive; cross-topic
    cosine ~= 0; unknown text -> "other" (orthogonal to all topics).
    """

    TOPICS = ["auth", "bugfix", "db", "style", "docs", "ratelimit"]

    KEYWORDS = [
        ("null pointer", "bugfix"),
        ("rate limit", "ratelimit"),
        ("connection pool", "db"),
        ("rate_limit", "ratelimit"),
        ("dark mode", "style"),
        ("dark theme", "style"),
        ("api example", "docs"),
        ("database", "db"),
        ("throttle", "ratelimit"),
        ("credential", "auth"),
        ("crash", "bugfix"),
        ("login", "auth"),
        ("token", "auth"),
        ("auth", "auth"),
        ("jwt", "auth"),
        ("css", "style"),
        ("theme", "style"),
        ("readme", "docs"),
        ("document", "docs"),
        ("pool", "db"),
        ("style", "style"),
    ]

    def __init__(self, dim: int = 8):
        self._dim = dim
        self._topic_vecs = {}
        for i, topic in enumerate(self.TOPICS):
            v = [0.0] * dim
            v[i] = 1.0
            self._topic_vecs[topic] = v
        self._other_vec = [0.0] * dim
        self._other_vec[len(self.TOPICS)] = 1.0

    def _classify(self, text: str) -> str:
        low = text.lower()
        for keyword, topic in self.KEYWORDS:
            if keyword in low:
                return topic
        return "other"

    def embed(self, text: str) -> list[float]:
        base = self._topic_vecs.get(self._classify(text), self._other_vec)
        h = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)
        perturb = 0.001 * ((h % 1000) / 1000.0 - 0.5)
        return [x + perturb for x in base]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    @property
    def dimension(self) -> int:
        return self._dim


AUTH_DIFF = """diff --git a/src/auth.py b/src/auth.py
index 111111..222222 100644
--- a/src/auth.py
+++ b/src/auth.py
@@ -1,3 +1,6 @@
 def login(user, password):
-    return check_db(user, password)
+    token = jwt.encode({"user": user}, SECRET)
+    return token
"""

DB_DIFF = """diff --git a/src/db/pool.py b/src/db/pool.py
index 333333..444444 100644
--- a/src/db/pool.py
+++ b/src/db/pool.py
@@ -1,3 +1,6 @@
 class ConnectionPool:
-    def __init__(self, size=5):
-        self._pool = [create_conn() for _ in range(size)]
+    def __init__(self, min_size=5, max_size=20):
+        self._pool = [create_conn() for _ in range(min_size)]
"""

STYLE_DIFF = """diff --git a/src/styles/theme.css b/src/styles/theme.css
index 555555..666666 100644
--- a/src/styles/theme.css
+++ b/src/styles/theme.css
@@ -1,3 +1,6 @@
+:root {
+  --color-bg: #0f172a;
+  --color-text: #e2e8f0;
+}
+
 body {
   background: white;
 }
"""

TWO_FILE_DIFF = """diff --git a/src/auth.py b/src/auth.py
index 111111..222222 100644
--- a/src/auth.py
+++ b/src/auth.py
@@ -1,3 +1,6 @@
+import jwt
+
 def login(user, password):
-    return check_db(user, password)
+    return jwt.encode({"user": user}, SECRET)
diff --git a/src/session.py b/src/session.py
index 333333..444444 100644
--- a/src/session.py
+++ b/src/session.py
@@ -10,3 +10,6 @@
 class Session:
     pass
+
+def create_session(user_id):
+    return Session(user_id=user_id)
"""

BINARY_DIFF = """diff --git a/img.png b/img.png
index 777777..888888 100644
Binary files a/img.png and b/img.png differ
"""


def _vec_bytes(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


@pytest.fixture
def storage(tmp_path):
    from live_edit.storage import SQLiteStorage

    return SQLiteStorage(str(tmp_path / "test_cross_session.db"))


@pytest.fixture
def embedder():
    return TopicFakeEmbedder(dim=8)


@pytest.fixture
def ltm(storage, embedder):
    cfg = LongTermConfig(
        enabled=True,
        similarity_threshold=0.6,
        recency_decay_rate=0.0,
        hit_count_weight=0.0,
        max_entries=10,
        max_stored_entries=5000,
    )
    return LongTermMemory(storage, embedder, cfg)


class TestTopicFakeEmbedder:
    def test_same_topic_high_cosine(self, embedder):
        a = embedder.embed("implement JWT token auth for login")
        b = embedder.embed("add jwt authentication to login flow")
        assert LongTermMemory._cosine_similarity(a, b) > 0.95

    def test_cross_topic_near_zero(self, embedder):
        a = embedder.embed("add JWT auth login")
        b = embedder.embed("add dark mode theme")
        assert LongTermMemory._cosine_similarity(a, b) < 0.1

    def test_other_orthogonal_to_topics(self, embedder):
        other = embedder.embed("unrelated task with no topic keyword")
        auth = embedder.embed("add JWT auth")
        assert LongTermMemory._cosine_similarity(other, auth) < 0.1

    def test_deterministic(self, embedder):
        assert embedder.embed("add JWT auth") == embedder.embed("add JWT auth")


class TestCrossUserRecall:
    async def test_b_retrieves_a_similar_edit(self, ltm):
        await ltm.store(
            "user_a:111",
            "implement JWT token auth for login endpoint",
            ["src/auth.py"],
            AUTH_DIFF,
            "h-a",
        )
        results = ltm.retrieve_sync("add jwt authentication to the login flow")
        assert results
        assert all(e.session_id.startswith("user_a:") for e in results)
        assert any(e.file_path for e in results)  # a file_diff chunk surfaced

    async def test_unrelated_query_returns_empty(self, ltm):
        await ltm.store(
            "user_a:111",
            "implement JWT token auth for login endpoint",
            ["src/auth.py"],
            AUTH_DIFF,
            "h-a",
        )
        assert ltm.retrieve_sync("unrelated task with no topic keyword") == []

    async def test_same_topic_a_and_b_both_surface(self, ltm):
        await ltm.store(
            "user_a:111",
            "implement JWT token auth for login endpoint",
            ["src/auth.py"],
            AUTH_DIFF,
            "h-a",
        )
        await ltm.store(
            "user_b:222",
            "refactor JWT auth to use refresh tokens",
            ["src/auth.py"],
            AUTH_DIFF,
            "h-b",
        )
        sids = {e.session_id for e in ltm.retrieve_sync("add jwt authentication")}
        assert any(s.startswith("user_a:") for s in sids)
        assert any(s.startswith("user_b:") for s in sids)


class TestCrossUserTopicFiltering:
    async def test_b_recalls_only_relevant_topic(self, ltm):
        await ltm.store(
            "user_a:111",
            "implement JWT token auth for login",
            ["src/auth.py"],
            AUTH_DIFF,
            "h-a",
        )
        await ltm.store(
            "user_b:222",
            "add dark mode theme to the UI",
            ["src/styles/theme.css"],
            STYLE_DIFF,
            "h-b",
        )
        results = ltm.retrieve_sync("jwt token auth login")
        assert results
        assert all(e.session_id.startswith("user_a:") for e in results)
        assert not any(e.session_id.startswith("user_b:") for e in results)


class TestContinuation:
    async def test_restore_replaces_chunks(self, ltm, storage):
        await ltm.store(
            "user_a:111", "implement JWT token auth", ["src/auth.py"], AUTH_DIFF, "h1"
        )
        count1 = len(storage.query_chunks())
        await ltm.store(
            "user_a:111", "implement JWT token auth", ["src/auth.py"], AUTH_DIFF, "h2"
        )
        count2 = len(storage.query_chunks())
        assert count1 == 2  # 1 request + 1 file_diff
        assert count2 == 2  # replaced, not duplicated

    async def test_continuation_recalls_own_history(self, ltm):
        await ltm.store(
            "user_a:111", "implement JWT token auth", ["src/auth.py"], AUTH_DIFF, "h1"
        )
        await ltm.store(
            "user_a:111", "implement JWT token auth", ["src/auth.py"], AUTH_DIFF, "h2"
        )
        results = ltm.retrieve_sync("add jwt auth")
        assert any(e.session_id.startswith("user_a:") for e in results)


class TestStoreBehavior:
    async def test_two_file_diff_chunks(self, ltm, storage):
        await ltm.store(
            "s1",
            "implement JWT token auth",
            ["src/auth.py", "src/session.py"],
            TWO_FILE_DIFF,
            "h1",
        )
        rows = storage.query_chunks()
        assert {c[3] for c in rows} == {"request", "file_diff"}
        assert sum(1 for c in rows if c[3] == "file_diff") == 2
        assert sum(1 for c in rows if c[3] == "request") == 1

    async def test_empty_diff_request_only(self, ltm, storage):
        await ltm.store("s1", "implement JWT token auth", ["src/auth.py"], "", "h1")
        rows = storage.query_chunks()
        assert len(rows) == 1
        assert rows[0][3] == "request"

    async def test_binary_diff_request_only(self, ltm, storage):
        await ltm.store("s1", "implement JWT token auth", [], BINARY_DIFF, "h1")
        rows = storage.query_chunks()
        assert len(rows) == 1
        assert rows[0][3] == "request"


class TestDisabledSwitch:
    def test_master_switch_noop(self, storage, embedder):
        cfg = MemoryConfig(enabled=False)
        mgr = MemoryManager(storage, embedder, cfg)
        mgr.store_sync("s1", "implement JWT auth", ["src/auth.py"], AUTH_DIFF, "h1")
        assert storage.query_chunks() == []
        msgs = [{"role": "user", "content": "add jwt auth"}]
        context, updated = mgr.retrieve_sync("add jwt auth", "s1", msgs, 1)
        assert context == ""
        assert updated is msgs


class TestScoring:
    def _ltm(self, storage, embedder, **overrides):
        defaults = {
            "enabled": True,
            "similarity_threshold": 0.0,
            "recency_decay_rate": 0.0,
            "hit_count_weight": 0.0,
            "max_entries": 10,
            "max_stored_entries": 5000,
        }
        defaults.update(overrides)
        return LongTermMemory(storage, embedder, LongTermConfig(**defaults))

    async def test_cross_topic_below_threshold_filtered(self, storage, embedder):
        ltm = self._ltm(storage, embedder, similarity_threshold=0.6)
        await ltm.store(
            "s1", "implement JWT token auth", ["src/auth.py"], AUTH_DIFF, "h1"
        )
        assert ltm.retrieve_sync("add dark mode theme to the UI") == []

    async def test_recency_decay_ranks_recent_first(self, storage, embedder):
        ltm = self._ltm(storage, embedder, recency_decay_rate=1.0)
        await ltm.store("old", "implement JWT token auth", ["src/auth.py"], AUTH_DIFF, "h-old")
        await ltm.store("new", "implement JWT token auth", ["src/auth.py"], AUTH_DIFF, "h-new")
        conn = storage._get_conn()
        conn.execute(
            "UPDATE session_chunks SET last_accessed = '2026-04-28T00:00:00' "
            "WHERE session_id = 'old'"
        )
        conn.commit()
        results = ltm.retrieve_sync("add jwt auth login")
        assert results
        assert results[0].session_id == "new"
        old = next(e for e in results if e.session_id == "old")
        assert old.score < 0.1

    async def test_hit_count_bonus_within_cap(self, storage, embedder):
        ltm = self._ltm(storage, embedder, hit_count_weight=0.05)
        await ltm.store("s1", "implement JWT token auth", ["src/auth.py"], AUTH_DIFF, "h1")
        conn = storage._get_conn()
        conn.execute("UPDATE session_chunks SET hit_count = 50 WHERE session_id = 's1'")
        conn.commit()
        results = ltm.retrieve_sync("add jwt auth")
        assert results
        assert results[0].score > 1.0  # bonus present
        assert results[0].score < 1.0 + 0.5 + 0.05  # capped at min(50,10)*0.05=0.5

    async def test_max_entries_truncation(self, storage, embedder):
        ltm = self._ltm(storage, embedder, max_entries=2)
        for i in range(3):
            await ltm.store(f"s{i}", "implement JWT token auth", ["src/auth.py"], AUTH_DIFF, f"h{i}")
        assert len(ltm.retrieve_sync("add jwt auth")) == 2

    async def test_per_session_top_two(self, storage, embedder):
        ltm = self._ltm(storage, embedder)
        await ltm.store(
            "s1",
            "implement JWT token auth",
            ["src/auth.py", "src/session.py"],
            TWO_FILE_DIFF,
            "h1",
        )
        s1 = [e for e in ltm.retrieve_sync("add jwt auth") if e.session_id == "s1"]
        assert len(s1) == 2

    async def test_file_diff_retained_over_request(self, storage, embedder):
        ltm = self._ltm(storage, embedder)
        await ltm.store(
            "s1",
            "implement JWT token auth",
            ["src/auth.py", "src/session.py"],
            TWO_FILE_DIFF,
            "h1",
        )
        results = [e for e in ltm.retrieve_sync("add jwt auth") if e.session_id == "s1"]
        assert len(results) == 2
        assert all(e.file_path for e in results)  # the two retained are the file_diffs


class TestContextFormatting:
    async def test_format_context_fields(self, storage, embedder):
        lcfg = LongTermConfig(
            enabled=True,
            similarity_threshold=0.6,
            recency_decay_rate=0.0,
            hit_count_weight=0.05,
            max_entries=5,
        )
        cfg = MemoryConfig(
            enabled=True,
            short_term=ShortTermConfig(
                max_full_rounds=10, max_stripped_rounds=10, max_summary_rounds=10
            ),
            long_term=lcfg,
            knowledge=KnowledgeConfig(enabled=False),
        )
        mgr = MemoryManager(storage, embedder, cfg)
        await mgr.store(
            "user_a:111",
            "implement JWT token auth for login",
            ["src/auth.py"],
            AUTH_DIFF,
            "h1",
        )
        conn = storage._get_conn()
        conn.execute("UPDATE session_chunks SET hit_count = 50 WHERE session_id = 'user_a:111'")
        conn.commit()
        msgs = [{"role": "user", "content": "add jwt auth"}]
        context, _ = mgr.retrieve_sync("add jwt auth", "user_b:222", msgs, round_num=1)
        assert "## Relevant Past Changes" in context
        assert "reference only" in context
        assert "implement JWT token auth" in context
        pcts = [int(m) for m in re.findall(r"\((\d+)%\)", context)]
        assert pcts and all(p <= 100 for p in pcts)  # score clamped <= 100%
        assert "100%" in context  # hit bonus pushed score above 1.0; clamp shows 100%, not 150%


class TestEviction:
    async def test_oldest_session_evicted(self, storage, embedder):
        ltm = LongTermMemory(
            storage,
            embedder,
            LongTermConfig(enabled=True, similarity_threshold=0.6, max_stored_entries=3),
        )
        for i in range(4):
            await ltm.store(f"s{i}", "implement JWT token auth", ["src/auth.py"], AUTH_DIFF, f"h{i}")
            await asyncio.sleep(1.1)
        sids = {c[1] for c in storage.query_chunks()}
        assert sids == {"s1", "s2", "s3"}


class TestRobustness:
    async def test_malformed_row_skipped_under_brute_force(
        self, storage, embedder, monkeypatch
    ):
        ltm = LongTermMemory(
            storage,
            embedder,
            LongTermConfig(enabled=True, similarity_threshold=0.6),
        )
        # Force the brute-force path (sqlite-vec not installed anyway).
        monkeypatch.setattr(storage, "query_chunks_vec", lambda *a, **k: None)
        await ltm.store(
            "user_a:111", "implement JWT token auth", ["src/auth.py"], AUTH_DIFF, "h1"
        )
        storage.store_chunks(
            "user_b:bad",
            "h-bad",
            [
                {
                    "chunk_type": "request",
                    "chunk_text": "add jwt auth",
                    "payload_json": json.dumps({"request": "add jwt auth"}),
                    "file_path": "",
                    "embedding_bytes": struct.pack("16f", *([0.5] * 16)),
                }
            ],
        )
        results = ltm.retrieve_sync("add jwt auth login")
        assert results
        assert all(e.session_id.startswith("user_a:") for e in results)
        assert not any(e.session_id.startswith("user_b:") for e in results)

    async def test_empty_db_returns_empty(self, storage, embedder):
        ltm = LongTermMemory(storage, embedder, LongTermConfig(enabled=True))
        assert ltm.retrieve_sync("add jwt auth") == []

    async def test_empty_query_safe(self, storage, embedder):
        ltm = LongTermMemory(storage, embedder, LongTermConfig(enabled=True))
        assert ltm.retrieve_sync("") == []


class TestRetrievalSideEffect:
    async def test_retrieve_bumps_hit_count(self, storage, embedder):
        ltm = LongTermMemory(
            storage,
            embedder,
            LongTermConfig(enabled=True, similarity_threshold=0.6),
        )
        await ltm.store(
            "user_a:111", "implement JWT token auth", ["src/auth.py"], AUTH_DIFF, "h1"
        )
        rows_before = storage.query_chunks()
        pinned = rows_before[0][0]  # chunk id
        assert rows_before[0][8] == 0  # hit_count
        ltm.retrieve_sync("add jwt auth login")
        rows_after = storage.query_chunks()
        matched = next(r for r in rows_after if r[0] == pinned)
        assert matched[8] == 1
        assert matched[9] is not None  # last_accessed refreshed


class TestL3FallbackAndMutualExclusion:
    def _mgr(self, storage, embedder):
        lcfg = LongTermConfig(
            enabled=True,
            similarity_threshold=0.6,
            recency_decay_rate=0.0,
            hit_count_weight=0.0,
            max_entries=5,
        )
        kcfg = KnowledgeConfig(enabled=True, max_entries=5)
        cfg = MemoryConfig(
            enabled=True,
            short_term=ShortTermConfig(
                max_full_rounds=10, max_stripped_rounds=10, max_summary_rounds=10
            ),
            long_term=lcfg,
            knowledge=kcfg,
        )
        return MemoryManager(storage, embedder, cfg)

    async def test_l2_empty_triggers_knowledge(self, storage, embedder):
        mgr = self._mgr(storage, embedder)
        mgr.add_knowledge(
            "api:db-tips", "database connection pool sizing and throughput guide", {}
        )
        msgs = [{"role": "user", "content": "database pool config"}]
        context, _ = mgr.retrieve_sync("database pool config", "user_b:222", msgs, round_num=1)
        assert "## Project Knowledge" in context

    async def test_l2_empty_triggers_knowledge_async(self, storage, embedder):
        mgr = self._mgr(storage, embedder)
        mgr.add_knowledge(
            "api:db-tips", "database connection pool sizing and throughput guide", {}
        )
        msgs = [{"role": "user", "content": "database pool config"}]
        context, _ = await mgr.retrieve(
            "database pool config", "user_b:222", msgs, round_num=1
        )
        assert "## Project Knowledge" in context

    async def test_l2_hit_suppresses_knowledge(self, storage, embedder):
        mgr = self._mgr(storage, embedder)
        mgr.add_knowledge(
            "api:db-tips", "database connection pool sizing and throughput guide", {}
        )
        await mgr.store(
            "user_a:111",
            "tune database connection pool settings",
            ["src/db/pool.py"],
            DB_DIFF,
            "h1",
        )
        msgs = [{"role": "user", "content": "database pool config"}]
        context, _ = mgr.retrieve_sync("database pool config", "user_b:222", msgs, round_num=1)
        assert "## Relevant Past Changes" in context
        assert "## Project Knowledge" not in context


class TestV1ToV2Migration:
    def test_migrated_chunks_retrievable(self, tmp_path, embedder):
        import sqlite3

        from live_edit.storage import SQLiteStorage

        db_path = str(tmp_path / "migrate.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL UNIQUE,
                request TEXT NOT NULL,
                files_json TEXT NOT NULL DEFAULT '[]',
                embedding BLOB NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                commit_hash TEXT NOT NULL DEFAULT '',
                chunk_type TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                file_path TEXT DEFAULT '',
                embedding BLOB NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()
        db_vec = _vec_bytes([0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0])  # db topic, dim 8
        conn.execute(
            "INSERT INTO session_embeddings (session_id, request, files_json, embedding) "
            "VALUES (?, ?, ?, ?)",
            ("user_a:legacy", "database connection pool tuning", '["src/db/pool.py"]', db_vec),
        )
        conn.commit()
        conn.close()

        storage = SQLiteStorage(db_path)
        ltm = LongTermMemory(
            storage, embedder, LongTermConfig(enabled=True, similarity_threshold=0.6)
        )
        results = ltm.retrieve_sync("connection pool settings")
        assert any(e.session_id == "user_a:legacy" for e in results)
        payload = json.loads(storage.query_chunks()[0][5])
        assert payload.get("migrated") is True
