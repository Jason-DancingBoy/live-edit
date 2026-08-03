import json  # noqa: F401  (kept verbatim from brief)
import os  # noqa: F401  (kept verbatim from brief)
import struct  # noqa: F401  (kept verbatim from brief)
import tempfile  # noqa: F401  (kept verbatim from brief)
from pathlib import Path  # noqa: F401  (kept verbatim from brief)
from unittest.mock import MagicMock, patch  # noqa: F401  (kept verbatim from brief)

import pytest

from live_edit.config import KnowledgeConfig
from live_edit.memory import KnowledgeBase, KnowledgeEntry


class FakeEmbedder:
    def __init__(self, dim=384):
        self._dim = dim

    def embed(self, text: str) -> list[float]:
        return [0.5] * self._dim

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.5] * self._dim for _ in texts]

    @property
    def dimension(self) -> int:
        return self._dim


class TestKnowledgeBase:
    @pytest.fixture
    def kb(self, tmp_path):
        from live_edit.storage import SQLiteStorage

        db = tmp_path / "test.db"
        store = SQLiteStorage(str(db))
        embedder = FakeEmbedder(dim=384)
        cfg = KnowledgeConfig(enabled=True, knowledge_dir=str(tmp_path / "knowledge"))
        return KnowledgeBase(store, embedder, cfg)

    def test_chunk_split_respects_chunk_size(self, kb):
        text = "word " * 300  # ~1500 chars
        chunks = kb._split_text(text, chunk_size=500, overlap=50)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 500 + 100  # tolerance for word boundaries

    def test_sync_files_adds_and_detects_changes(self, kb, tmp_path):
        kb_dir = tmp_path / "knowledge"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "doc1.md").write_text("# Test Doc\n\nThis is a test document.")

        result = kb.sync_files(str(tmp_path))
        assert result["added"] == 1

        meta = kb._storage.list_knowledge_meta()
        assert len(meta) == 1

        # Modify the file
        (kb_dir / "doc1.md").write_text("# Updated Doc\n\nCompletely different content.")
        result2 = kb.sync_files(str(tmp_path))
        assert result2["updated"] == 1

    def test_sync_files_removes_deleted_files(self, kb, tmp_path):
        kb_dir = tmp_path / "knowledge"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "temp.md").write_text("temporary")

        kb.sync_files(str(tmp_path))
        assert kb._storage.list_knowledge_meta()

        (kb_dir / "temp.md").unlink()
        result = kb.sync_files(str(tmp_path))
        assert result["removed"] == 1

    def test_search_returns_relevant_chunks(self, kb, tmp_path):
        kb_dir = tmp_path / "knowledge"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "style.md").write_text("Use 4-space indentation for all Python files.")

        kb.sync_files(str(tmp_path))
        results = kb.search("python indentation")
        assert len(results) > 0
        assert isinstance(results[0], KnowledgeEntry)
        assert "indentation" in results[0].chunk_text.lower()

    def test_add_api_document(self, kb):
        kb.add_api_document("api:rules", "All commits must be signed.", {"tag": "git"})
        meta = kb._storage.list_knowledge_meta()
        assert any(m["source_path"] == "api:rules" for m in meta)
        assert any(m["source_type"] == "api" for m in meta)

    def test_delete_api_document_works(self, kb):
        kb.add_api_document("api:tmp", "delete me", {})
        kb.delete_document("api:tmp")
        assert not kb._storage.list_knowledge_meta()

    def test_delete_file_document_rejected(self, kb, tmp_path):
        kb_dir = tmp_path / "knowledge"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "keep.md").write_text("important")
        kb.sync_files(str(tmp_path))

        with pytest.raises(ValueError, match="file"):
            kb.delete_document("keep.md")

    def test_list_documents(self, kb):
        kb.add_api_document("api:a", "content a", {})
        kb.add_api_document("api:b", "content b", {})
        docs = kb.list_documents()
        paths = {d["source_path"] for d in docs}
        assert "api:a" in paths
        assert "api:b" in paths

    def test_split_text_no_duplicate_after_oversized_paragraph(self, kb):
        para = "x" * 1200
        text = "SHORT_PARA_AAA\n\n" + para + "\n\nSHORT_PARA_BBB"
        chunks = kb._split_text(text, chunk_size=500, overlap=50)
        assert len(chunks) > 1
        joined = "\n".join(chunks)
        assert joined.count("SHORT_PARA_AAA") == 1
        assert joined.count("SHORT_PARA_BBB") == 1

    def test_sync_files_skips_failed_file_and_continues(self, kb, tmp_path):
        kb_dir = tmp_path / "knowledge"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "a.md").write_text("content a")
        (kb_dir / "b.md").write_text("content b")

        orig = kb._index_document
        calls = {"n": 0}

        def flaky(src, content, stype, fhash):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return orig(src, content, stype, fhash)

        kb._index_document = flaky

        result = kb.sync_files(str(tmp_path))
        assert result["added"] == 1  # only b.md indexed; a.md skipped, not aborted
