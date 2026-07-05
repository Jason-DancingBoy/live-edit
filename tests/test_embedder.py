"""Tests for live_edit.embedder -- Embedder ABC and LocalEmbedder."""

import sys
import threading

import numpy as np
import pytest
from unittest.mock import MagicMock, patch

from live_edit.embedder import Embedder, LocalEmbedder


class TestEmbedderABC:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            Embedder()

    def test_concrete_subclass_must_implement_embed_and_dimension(self):
        class Incomplete(Embedder):
            pass
        with pytest.raises(TypeError):
            Incomplete()

    def test_embed_batch_default_loops_embed(self):
        class SimpleEmbedder(Embedder):
            def embed(self, text):
                return [len(text), float(ord(text[0])) if text else 0.0]

            @property
            def dimension(self):
                return 2

        e = SimpleEmbedder()
        results = e.embed_batch(["hi", "bye"])
        assert len(results) == 2
        assert results[0] == [2.0, float(ord("h"))]
        assert results[1] == [3.0, float(ord("b"))]


class TestLocalEmbedder:
    @pytest.fixture(autouse=True)
    def _mock_sentence_transformers_module(self):
        """Insert a mock sentence_transformers module so patch() can resolve it.

        Without this, ``patch("sentence_transformers.SentenceTransformer")``
        would fail because sentence-transformers is not installed.
        """
        sentinel = object()
        key = "sentence_transformers"
        original = sys.modules.get(key, sentinel)
        if key not in sys.modules:
            sys.modules[key] = MagicMock()
        yield
        if original is sentinel:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = original

    @pytest.fixture
    def mock_sentence_transformer(self):
        with patch("sentence_transformers.SentenceTransformer") as mock_st:
            mock_model = MagicMock()
            mock_model.encode.return_value = np.array(
                [0.1, 0.2, 0.3], dtype=np.float32
            )
            mock_st.return_value = mock_model
            yield mock_st, mock_model

    def test_dimension_is_384(self, mock_sentence_transformer):
        mock_st, mock_model = mock_sentence_transformer
        mock_model.get_sentence_embedding_dimension.return_value = 384
        e = LocalEmbedder(model_name="all-MiniLM-L6-v2")
        assert e.dimension == 384

    def test_embed_returns_list_of_floats(self, mock_sentence_transformer):
        mock_st, mock_model = mock_sentence_transformer
        mock_model.get_sentence_embedding_dimension.return_value = 384
        mock_model.encode.return_value = np.array(
            [0.1, 0.2, 0.3], dtype=np.float32
        )
        e = LocalEmbedder(model_name="test-model")
        result = e.embed("hello world")
        assert isinstance(result, list)
        assert len(result) == 3
        assert all(isinstance(v, float) for v in result)

    def test_lazy_loading_loads_model_on_first_call(
        self, mock_sentence_transformer
    ):
        mock_st, mock_model = mock_sentence_transformer
        mock_model.get_sentence_embedding_dimension.return_value = 384
        mock_model.encode.return_value = np.array([0.1, 0.2], dtype=np.float32)
        e = LocalEmbedder(model_name="test-model")
        mock_st.assert_not_called()
        e.embed("first call")
        mock_st.assert_called_once_with("test-model")

    def test_lazy_loading_only_loads_once(self, mock_sentence_transformer):
        mock_st, mock_model = mock_sentence_transformer
        mock_model.get_sentence_embedding_dimension.return_value = 384
        mock_model.encode.return_value = np.array([0.1, 0.2], dtype=np.float32)
        e = LocalEmbedder(model_name="test-model")
        e.embed("first")
        e.embed("second")
        assert mock_st.call_count == 1

    def test_thread_safety_during_init(self):
        mock_st_cls = MagicMock()
        mock_model = MagicMock()
        mock_model.get_sentence_embedding_dimension.return_value = 384
        mock_model.encode.return_value = np.array([0.1], dtype=np.float32)
        mock_st_cls.return_value = mock_model

        with patch("sentence_transformers.SentenceTransformer", mock_st_cls):
            e = LocalEmbedder(model_name="test")
            results = []
            errors = []

            def call_embed():
                try:
                    results.append(e.embed("thread"))
                except Exception as ex:
                    errors.append(ex)

            t1 = threading.Thread(target=call_embed)
            t2 = threading.Thread(target=call_embed)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        assert len(results) == 2
        assert len(errors) == 0
        assert mock_st_cls.call_count == 1

    def test_embed_batch_uses_native_batch(self, mock_sentence_transformer):
        mock_st, mock_model = mock_sentence_transformer
        mock_model.get_sentence_embedding_dimension.return_value = 384
        mock_model.encode.return_value = np.array(
            [[0.1], [0.2]], dtype=np.float32
        )
        e = LocalEmbedder(model_name="test-model")
        results = e.embed_batch(["text1", "text2"])
        assert len(results) == 2
        mock_model.encode.assert_called_once_with(["text1", "text2"])
