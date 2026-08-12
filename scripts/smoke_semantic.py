#!/usr/bin/env python3
"""Real-model smoke test for L2 long-term memory.

Runs the project's real L2 pipeline (LongTermMemory.store -> SQLiteStorage
-> retrieve_sync -> _score_and_rank, all live_edit/ code) with a REAL
semantic model, then prints the retrieved entries with their true cosine
scores to show that matching is semantic, not keyword-based.

Embedder selection (two modes):

1. LocalEmbedder (live_edit/embedder.py) -- the exact production code path.
   Requires torch + sentence-transformers:
       .venv/bin/pip install torch transformers sentence-transformers

2. OnnxEmbedder (defined below) -- fallback when torch is unavailable.
   Loads the SAME thenlper/gte-small model from its official ONNX export
   (onnx/model.onnx) with onnxruntime + tokenizers. Runs the same real L2
   pipeline; only the embedder call differs.

HuggingFace is unreachable from this machine, so weights come from the
mirror. HF_ENDPOINT defaults to https://hf-mirror.com; override via env.
The ONNX files (~133MB) cache in ~/.cache/live-edit-gte-small on first run.

Run:
    HF_ENDPOINT=https://hf-mirror.com .venv/bin/python scripts/smoke_semantic.py
"""

import asyncio
import logging
import os
import sys
import tempfile
import urllib.request

# huggingface.co 直连不通，默认走镜像；允许用环境变量覆盖
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 三个"历史会话"：语义上分属 auth / db / ui 三类，关键词几乎不重叠。
DEMO_SESSIONS = [
    {
        "session_id": "demo:auth",
        "request": "add JWT token authentication to the login endpoint",
        "files": ["auth/login.py"],
        "diff": (
            "diff --git a/auth/login.py b/auth/login.py\n"
            "--- a/auth/login.py\n+++ b/auth/login.py\n"
            "@@ -10,0 +11,4 @@\n"
            "+    token = jwt.encode(payload, SECRET, algorithm='HS256')\n"
            "+    return {'token': token}\n"
        ),
        "commit_hash": "aaaa1111",
    },
    {
        "session_id": "demo:db",
        "request": "migrate the users table to add a phone_number column",
        "files": ["db/migrate.py"],
        "diff": (
            "diff --git a/db/migrate.py b/db/migrate.py\n"
            "--- a/db/migrate.py\n+++ b/db/migrate.py\n"
            "@@ -5,0 +6,2 @@\n"
            "+    conn.execute('ALTER TABLE users ADD COLUMN phone_number TEXT')\n"
            "+    conn.commit()\n"
        ),
        "commit_hash": "cccc3333",
    },
    {
        "session_id": "demo:ui",
        "request": "change the dashboard sidebar background color to dark blue",
        "files": ["web/sidebar.css"],
        "diff": (
            "diff --git a/web/sidebar.css b/web/sidebar.css\n"
            "--- a/web/sidebar.css\n+++ b/web/sidebar.css\n"
            "@@ -1,0 +1,2 @@\n+    background-color: #0a1f44;\n"
        ),
        "commit_hash": "bbbb2222",
    },
]

# (查询, 期望命中的会话)：与历史会话语义相近、但用词几乎不重叠，
# 用于证明匹配靠"语义"而非"关键词"。
DEMO_QUERIES = [
    ("implement OAuth login session security", "demo:auth"),
    ("add a column to store each user phone number", "demo:db"),
    ("make the navigation bar look modern and elegant", "demo:ui"),
]

GTE_SMALL_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "live-edit-gte-small")


class OnnxEmbedder:
    """torch-free loader for the same thenlper/gte-small model (ONNX export).

    Mirrors the production LocalEmbedder contract: embed / embed_batch /
    dimension. Replicates the sentence-transformers pipeline for this model:
    mean pooling over the attention mask + L2 normalize (the repo ships a
    2_Normalize module).
    """

    def __init__(self, model_dir=None):
        import onnxruntime
        from tokenizers import Tokenizer

        model_dir = model_dir or _ensure_model_dir()
        self._session = onnxruntime.InferenceSession(
            os.path.join(model_dir, "model.onnx"), providers=["CPUExecutionProvider"]
        )
        self._tokenizer = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self._dimension = 384

    def embed(self, text: str) -> list[float]:
        import numpy as np

        enc = self._tokenizer.encode(text)
        ids = np.array([enc.ids], dtype=np.int64)
        mask = np.array([enc.attention_mask], dtype=np.int64)
        tt = np.array([enc.type_ids], dtype=np.int64)
        (last_hidden,) = self._session.run(
            None, {"input_ids": ids, "attention_mask": mask, "token_type_ids": tt}
        )
        m = mask[0].astype(np.float32)[:, None]
        pooled = (last_hidden[0] * m).sum(0) / m.sum()
        norm = np.linalg.norm(pooled)
        return (pooled / norm if norm else pooled).tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    @property
    def dimension(self) -> int:
        return self._dimension


def _ensure_model_dir() -> str:
    os.makedirs(GTE_SMALL_CACHE, exist_ok=True)
    for fname in ("model.onnx", "tokenizer.json"):
        dest = os.path.join(GTE_SMALL_CACHE, fname)
        if not os.path.exists(dest):
            base = os.environ["HF_ENDPOINT"].rstrip("/")
            url = f"{base}/thenlper/gte-small/resolve/main/{fname}"
            print(f"[*] downloading {fname} from {base} ...")
            urllib.request.urlretrieve(url, dest)
    return GTE_SMALL_CACHE


def _make_embedder():
    try:
        import sentence_transformers  # noqa: F401  (探针：torch 是否可用)

        from live_edit.embedder import LocalEmbedder

        print("[*] torch/sentence-transformers available -> LocalEmbedder (exact production code)")
        return LocalEmbedder()
    except ImportError:
        print("[*] torch unavailable -> OnnxEmbedder (same thenlper/gte-small, onnx export)")
        try:
            return OnnxEmbedder()
        except ImportError as exc:
            print(f"[FAIL] onnxruntime/tokenizers not installed: {exc}")
            print("Install: .venv/bin/pip install onnxruntime tokenizers")
            raise SystemExit(1) from exc


async def _store_demo(ltm) -> None:
    print("[*] storing demo sessions ...")
    for s in DEMO_SESSIONS:
        await ltm.store(s["session_id"], s["request"], s["files"], s["diff"], s["commit_hash"])
        print(f"  stored {s['session_id']}")


def _run() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    from live_edit.memory import LongTermConfig, LongTermMemory
    from live_edit.storage import SQLiteStorage

    print(f"[*] HF_ENDPOINT = {os.environ['HF_ENDPOINT']}")
    embedder = _make_embedder()
    print(f"[OK] embedder ready, dim={embedder.dimension}")

    tmp = tempfile.mkdtemp(prefix="smoke_semantic_")
    storage = SQLiteStorage(os.path.join(tmp, "smoke.db"))
    vec_status = "installed" if storage._vec_loaded else "not installed (brute-force fallback)"
    print(f"[*] sqlite-vec: {vec_status}")

    config = LongTermConfig(
        enabled=True,
        max_entries=10,
        similarity_threshold=0.0,  # 关掉过滤，展示完整相似度排序
        max_stored_entries=5000,
        recency_decay_rate=0.0,  # 关掉时间衰减
        hit_count_weight=0.0,  # 关掉命中加成 -> score 即为纯余弦
    )
    ltm = LongTermMemory(storage, embedder, config)

    asyncio.run(_store_demo(ltm))

    print("\n=== store -> retrieve 结果（score = 真实余弦相似度） ===")
    for query, expected in DEMO_QUERIES:
        entries = ltm.retrieve_sync(query)
        print(f"\nquery: {query!r}")
        if not entries:
            print("  [FAIL] no memories retrieved (store 可能静默失败，见上方日志)")
            continue
        for e in entries:
            hit = "HIT " if e.session_id == expected else "miss"
            print(f"  [{hit}] score={e.score:.3f}  session={e.session_id}  request={e.request!r}")

    print("\n备注: gte-small 的短句绝对余弦普遍偏高（~0.7 起），属模型特性；")
    print("      看相对排序而非绝对数值——每个 query 的 HIT 会话应排在最前。")
    print(f"\n[OK] DB at {storage.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
