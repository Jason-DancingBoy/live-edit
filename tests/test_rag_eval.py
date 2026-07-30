"""RAG evaluation framework — ground-truth query→session pairs and recall@k metrics.

Usage:
  # CI mode (deterministic FakeEmbedder)
  python3 -m pytest tests/test_rag_eval.py -v

  # Real model evaluation
  python3 -c "
import asyncio
from tests.test_rag_eval import run_real_eval
asyncio.run(run_real_eval())
"
"""

import asyncio
import tempfile

import pytest

from live_edit.config import SessionMemoryConfig
from live_edit.session_memory import SessionMemory

# ---------------------------------------------------------------------------
# Eval dataset
# ---------------------------------------------------------------------------

EVAL_SESSIONS = [
    (
        "s1",
        "Add JWT authentication to login endpoint",
        ["src/auth.py", "src/middleware.py"],
        """\
diff --git a/src/auth.py b/src/auth.py
index abc123..def456 100644
--- a/src/auth.py
+++ b/src/auth.py
@@ -1,5 +1,12 @@
 import os
+import jwt
+from datetime import datetime, timedelta
+
-def login(user, password):
-    return check_db(user, password)
+SECRET = os.environ["JWT_SECRET"]
+
+def login(user, password):
+    if not check_db(user, password):
+        raise AuthError("invalid credentials")
+    token = jwt.encode({"sub": user, "exp": datetime.utcnow() + timedelta(hours=1)}, SECRET)
+    return {"token": token}
diff --git a/src/middleware.py b/src/middleware.py
index 111222..333444 100644
--- a/src/middleware.py
+++ b/src/middleware.py
@@ -1,3 +1,8 @@
-def handle(req):
+import jwt
+import os
+
+def handle(req):
+    token = req.headers.get("Authorization", "").replace("Bearer ", "")
+    if not token:
+        raise AuthError("missing token")
+    payload = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=["HS256"])
+    req.user = payload["sub"]
     return req
""",
    ),
    (
        "s2",
        "Fix null pointer crash in user profile page",
        ["src/components/UserProfile.tsx"],
        """\
diff --git a/src/components/UserProfile.tsx b/src/components/UserProfile.tsx
index abc..def 100644
--- a/src/components/UserProfile.tsx
+++ b/src/components/UserProfile.tsx
@@ -5,6 +5,8 @@ export function UserProfile({ userId }: Props) {
   const [user, setUser] = useState<User | null>(null);

   useEffect(() => {
-    fetchUser(userId).then(setUser);
+    fetchUser(userId)
+      .then(data => setUser(data))
+      .catch(err => setUser(null));
   }, [userId]);

-  return <div>{user.name}</div>;
+  return <div>{user?.name ?? 'Unknown'}</div>;
 }
""",
    ),
    (
        "s3",
        "Refactor database connection pool for better throughput",
        ["src/db/pool.py"],
        """\
diff --git a/src/db/pool.py b/src/db/pool.py
index 111..222 100644
--- a/src/db/pool.py
+++ b/src/db/pool.py
@@ -1,8 +1,12 @@
+from queue import Queue
+import threading
+
 class ConnectionPool:
-    def __init__(self, size=5):
-        self._pool = [create_conn() for _ in range(size)]
+    def __init__(self, min_size=5, max_size=20):
+        self._min = min_size
+        self._max = max_size
+        self._pool = Queue(maxsize=max_size)
+        self._lock = threading.Lock()
+        for _ in range(min_size):
+            self._pool.put(create_conn())

     def acquire(self):
-        return self._pool.pop()
+        with self._lock:
+            if self._pool.empty() and self._count < self._max:
+                return create_conn()
+            return self._pool.get()
""",
    ),
    (
        "s4",
        "Add CSS custom properties for dark mode theme",
        ["src/styles/theme.css"],
        """\
diff --git a/src/styles/theme.css b/src/styles/theme.css
index aaa..bbb 100644
--- a/src/styles/theme.css
+++ b/src/styles/theme.css
@@ -1,3 +1,12 @@
+:root {
+  --color-bg: #ffffff;
+  --color-text: #1a1a1a;
+  --color-primary: #3b82f6;
+}
+
+[data-theme="dark"] {
+  --color-bg: #0f172a;
+  --color-text: #e2e8f0;
+  --color-primary: #60a5fa;
+}
+
 body {
-  background: white;
-  color: black;
+  background: var(--color-bg);
+  color: var(--color-text);
 }
""",
    ),
    (
        "s5",
        "Update README with new API endpoint examples",
        ["README.md"],
        """\
diff --git a/README.md b/README.md
index xxx..yyy 100644
--- a/README.md
+++ b/README.md
@@ -10,3 +10,15 @@
 ## Usage

-Run `python main.py`.
+### Authentication
+
+```python
+import requests
+r = requests.post("https://api.example.com/auth/login", json={"user": "alice", "password": "s3cret"})
+token = r.json()["token"]
+```
+
+### Create Item
+
+```python
+headers = {"Authorization": f"Bearer {token}"}
+r = requests.post("https://api.example.com/items", json={"name": "widget"}, headers=headers)
+```
""",
    ),
    (
        "s6",
        "Add rate limiting middleware for API routes",
        ["src/middleware/rate_limit.py"],
        """\
diff --git a/src/middleware/rate_limit.py b/src/middleware/rate_limit.py
new file mode 100644
index 0000000..abcdef1
--- /dev/null
+++ b/src/middleware/rate_limit.py
@@ -0,0 +1,15 @@
+import time
+from collections import defaultdict
+
+class RateLimiter:
+    def __init__(self, max_requests=100, window_seconds=60):
+        self._max = max_requests
+        self._window = window_seconds
+        self._buckets = defaultdict(list)
+
+    def allow(self, client_id):
+        now = time.time()
+        bucket = self._buckets[client_id]
+        bucket[:] = [t for t in bucket if now - t < self._window]
+        if len(bucket) >= self._max:
+            return False
+        bucket.append(now)
+        return True
""",
    ),
]

EVAL_QUERIES = [
    ("implement token-based auth for login", "s1"),
    ("fix crash on user profile page", "s2"),
    ("optimize database connection pool settings", "s3"),
    ("add dark theme CSS support", "s4"),
    ("document how to use the API", "s5"),
    ("throttle API requests with rate limiting", "s6"),
    ("add session cookie handling", "s1"),
    ("make the connection pool faster", "s3"),
]


# ---------------------------------------------------------------------------
# Deterministic FakeEmbedder for CI
# ---------------------------------------------------------------------------

TOPIC_VECTORS = {
    "auth": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "bugfix": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
    "db": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
    "style": [0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
    "docs": [0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
    "ratelimit": [0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
}

TOPIC_KEYWORDS = {
    "auth": ["auth", "jwt", "token", "login", "session cookie", "credential"],
    "bugfix": ["crash", "null pointer", "fix crash", "profile page", "user profile"],
    "db": ["database", "connection pool", "throughput", "db/pool"],
    "style": ["css", "dark mode", "dark theme", "theme.css", "style"],
    "docs": ["readme", "document", "api example", "README"],
    "ratelimit": ["rate limit", "throttle", "rate_limit"],
}

DIM = 6


def _topic(text: str) -> str:
    text_lower = text.lower()
    for topic, keywords in sorted(TOPIC_KEYWORDS.items(), key=lambda x: -max(len(k) for k in x[1])):
        for kw in keywords:
            if kw in text_lower:
                return topic
    return "auth"


class EvalFakeEmbedder:
    def __init__(self):
        self._dim = DIM

    def embed(self, text: str) -> list[float]:
        return TOPIC_VECTORS[_topic(text)]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    @property
    def dimension(self) -> int:
        return self._dim


# ---------------------------------------------------------------------------
# Fake storage for eval
# ---------------------------------------------------------------------------


class EvalFakeStorage:
    def __init__(self):
        self._chunks = []

    def store_chunks(self, session_id, commit_hash, chunks):
        self._chunks = [c for c in self._chunks if c["_sid"] != session_id]
        for c in chunks:
            c["_sid"] = session_id
            c["_hash"] = commit_hash
        self._chunks.extend(chunks)

    def query_chunks(self, limit=15000):
        results = []
        for i, c in enumerate(self._chunks[-limit:]):
            emb = c.get("embedding_bytes", b"")
            results.append(
                (
                    i,
                    c.get("_sid", ""),
                    c.get("_hash", ""),
                    c.get("chunk_type", ""),
                    c.get("chunk_text", ""),
                    c.get("payload_json", "{}"),
                    c.get("file_path", ""),
                    emb,
                )
            )
        return results

    def delete_old_sessions(self, keep_count):
        sessions = {}
        for i, c in enumerate(self._chunks):
            sid = c.get("_sid", "")
            if sid not in sessions:
                sessions[sid] = i
        keep_sids = set(sorted(sessions, key=lambda s: sessions[s])[-keep_count:])
        self._chunks = [c for c in self._chunks if c.get("_sid") in keep_sids]

    def get_db_version(self):
        return 1

    def set_db_version(self, v):
        pass


# ---------------------------------------------------------------------------
# Eval runner
# ---------------------------------------------------------------------------


async def store_sessions(sm, sessions):
    for sid, request, files, diff in sessions:
        await sm.store(sid, request, files, diff, f"hash-{sid}")


async def run_queries(sm, queries):
    results = {}
    for query_text, expected_sid in queries:
        entries = await sm.retrieve(query_text)
        results[query_text] = {
            "expected": expected_sid,
            "retrieved": [e.session_id for e in entries],
            "scores": [e.score for e in entries],
        }
    return results


def compute_metrics(query_results, k_values=(1, 3, 5)):
    metrics = {}
    for k in k_values:
        hits = 0
        for _query_text, result in query_results.items():
            retrieved = result["retrieved"][:k]
            if result["expected"] in retrieved:
                hits += 1
        metrics[f"recall@{k}"] = hits / len(query_results) if query_results else 0.0
    return metrics


def print_eval_report(query_results):
    metrics = compute_metrics(query_results)
    print("\n" + "=" * 60)
    print("RAG Evaluation Report")
    print("=" * 60)
    for query_text, result in query_results.items():
        status = "HIT" if result["expected"] in result["retrieved"][:3] else "MISS"
        print(f"\n[{status}] Query: {query_text}")
        print(f"       Expected: {result['expected']}")
        retrieved = result["retrieved"][:5]
        scores = result["scores"][:5]
        for i, (sid, score) in enumerate(zip(retrieved, scores, strict=True)):
            marker = "<<<" if sid == result["expected"] else ""
            print(f"       #{i + 1}: {sid} ({score:.3f}) {marker}")
    print("\n--- Metrics ---")
    for name, value in metrics.items():
        print(f"  {name}: {value:.1%}")
    return metrics


# ---------------------------------------------------------------------------
# CI Tests
# ---------------------------------------------------------------------------


class TestRagEval:
    @pytest.fixture
    def sm(self):
        storage = EvalFakeStorage()
        embedder = EvalFakeEmbedder()
        config = SessionMemoryConfig(
            enabled=True,
            max_entries=10,
            similarity_threshold=0.5,
            max_stored_entries=100,
        )
        return SessionMemory(storage, embedder, config)

    def test_recall_at_1(self, sm):
        async def run():
            await store_sessions(sm, EVAL_SESSIONS)
            results = await run_queries(sm, EVAL_QUERIES)
            return compute_metrics(results)

        metrics = asyncio.run(run())
        assert metrics["recall@1"] >= 0.75  # 6/8 minimum

    def test_recall_at_3(self, sm):
        async def run():
            await store_sessions(sm, EVAL_SESSIONS)
            results = await run_queries(sm, EVAL_QUERIES)
            return compute_metrics(results)

        metrics = asyncio.run(run())
        assert metrics["recall@3"] >= 0.875  # 7/8 minimum

    def test_recall_at_5(self, sm):
        async def run():
            await store_sessions(sm, EVAL_SESSIONS)
            results = await run_queries(sm, EVAL_QUERIES)
            return compute_metrics(results)

        metrics = asyncio.run(run())
        assert metrics["recall@5"] == 1.0  # all queries should hit

    def test_eval_report_runs(self, sm):
        async def run():
            await store_sessions(sm, EVAL_SESSIONS)
            return await run_queries(sm, EVAL_QUERIES)

        results = asyncio.run(run())
        metrics = print_eval_report(results)
        assert len(metrics) == 3

    def test_multiple_sessions_ranking(self, sm):
        """Verify same-topic sessions are ranked by chunk-level score."""

        async def run():
            await store_sessions(sm, EVAL_SESSIONS)
            return await sm.retrieve("implement token-based auth for login")

        entries = asyncio.run(run())
        sids = [e.session_id for e in entries]
        # s1 (auth topic) should appear, s4 (style topic) should not
        assert "s1" in sids
        assert "s4" not in sids


# ---------------------------------------------------------------------------
# Real-model evaluation (run manually)
# ---------------------------------------------------------------------------


async def run_real_eval(model_name="thenlper/gte-small"):
    """Run eval with a real LocalEmbedder model. Requires sentence-transformers."""
    from live_edit.embedder import LocalEmbedder
    from live_edit.storage import SQLiteStorage

    print(f"Loading model: {model_name}")
    embedder = LocalEmbedder(model_name=model_name)
    embedder.embed("warmup")

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        storage = SQLiteStorage(f.name)
        config = SessionMemoryConfig(
            enabled=True,
            max_entries=10,
            similarity_threshold=0.4,
            max_stored_entries=100,
        )
        sm = SessionMemory(storage, embedder, config)

        print(f"Storing {len(EVAL_SESSIONS)} sessions...")
        await store_sessions(sm, EVAL_SESSIONS)

        print(f"Running {len(EVAL_QUERIES)} queries...")
        results = await run_queries(sm, EVAL_QUERIES)

        return print_eval_report(results)
