"""Embedder abstract interface and default LocalEmbedder implementation."""

import logging
import threading
from abc import ABC, abstractmethod

logger = logging.getLogger("live-edit.embedder")


class Embedder(ABC):
    """Abstract interface for text-to-vector embedding."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return a float vector for a single text."""

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return float vectors for multiple texts.

        Default loops embed(). Override for optimized batch inference.
        """
        return [self.embed(t) for t in texts]

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Return the embedding vector dimension."""


class LocalEmbedder(Embedder):
    """Default embedder using sentence-transformers (all-MiniLM-L6-v2).

    Lazy-loads the model on first call. Thread-safe initialization.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self._model_name = model_name
        self._model = None
        self._dimension = 0
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
            self._dimension = self._model.get_sentence_embedding_dimension()
            logger.info(
                "LocalEmbedder loaded model=%s dim=%d",
                self._model_name,
                self._dimension,
            )

    def embed(self, text: str) -> list[float]:
        self._ensure_loaded()
        vec = self._model.encode(text)
        return vec.tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._ensure_loaded()
        vecs = self._model.encode(texts)
        return vecs.tolist()

    @property
    def dimension(self) -> int:
        self._ensure_loaded()
        return self._dimension
