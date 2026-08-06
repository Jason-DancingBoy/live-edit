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
