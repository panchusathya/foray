"""Pluggable text embeddings.

``hashing`` (default) is a deterministic, dependency-free feature-hashing
embedder: unigrams + bigrams -> signed hashed buckets -> L2 normalised. It runs
offline, is stable across processes, and is good enough for the semantic half of
hybrid retrieval at demo scale.

``voyage`` calls Voyage AI when VOYAGE_API_KEY is set. The interface is one
method, so swapping in another provider is a single class.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import math
from typing import Protocol

import numpy as np

from ..config import get_settings
from ..enrich.taxonomy import slugify_text

logger = logging.getLogger(__name__)

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "by", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "that", "this", "these", "those", "it", "its", "i", "we", "you", "they",
    "our", "their", "his", "her", "my", "me", "us", "them", "he", "she",
    "have", "has", "had", "do", "does", "did", "will", "would", "can", "could",
    "should", "may", "might", "must", "not", "no", "s", "t",
}


def tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, drop stopwords and single characters."""
    slug = slugify_text(text)
    if not slug:
        return []
    return [
        token
        for token in slug.split()
        if token not in STOPWORDS and (len(token) > 1 or token.isdigit())
    ]


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, texts: list[str], *, input_type: str = "document") -> np.ndarray:  # pragma: no cover - protocol
        ...


class HashingEmbedder:
    """Signed feature hashing over unigrams and bigrams."""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}"

    @staticmethod
    def _bucket(token: str, dim: int) -> tuple[int, float]:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        index = value % dim
        sign = 1.0 if (value >> 63) & 1 else -1.0
        return index, sign

    def embed_one(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=np.float32)
        tokens = tokenize(text)
        if not tokens:
            return vector

        counts: dict[str, float] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0.0) + 1.0
        for left, right in itertools.pairwise(tokens):
            bigram = f"{left}_{right}"
            counts[bigram] = counts.get(bigram, 0.0) + 0.6

        for token, count in counts.items():
            index, sign = self._bucket(token, self.dim)
            # Sub-linear term weighting keeps long profiles from dominating.
            vector[index] += sign * (1.0 + math.log(count))

        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector /= norm
        return vector

    def embed(self, texts: list[str], *, input_type: str = "document") -> np.ndarray:
        del input_type  # hashing is lexical; query vs document is a no-op
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self.embed_one(text) for text in texts])


class VoyageEmbedder:
    """Voyage AI embeddings, with a hashing fallback if the call fails."""

    def __init__(self, api_key: str, model: str, dim: int) -> None:
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.name = f"voyage-{model}"
        self._fallback = HashingEmbedder(dim)

    def embed(self, texts: list[str], *, input_type: str = "document") -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        kind = input_type if input_type in {"document", "query"} else "document"
        try:
            import httpx

            response = httpx.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts, "input_type": kind},
                timeout=60.0,
            )
            response.raise_for_status()
            payload = response.json()
            vectors = [item["embedding"] for item in payload["data"]]
            matrix = np.asarray(vectors, dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self.dim = matrix.shape[1]
            return matrix / norms
        except Exception as exc:  # noqa: BLE001 - never fail a search on embeddings
            logger.warning("voyage embedding failed, falling back to hashing: %s", exc)
            return self._fallback.embed(texts)


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        settings = get_settings()
        if settings.embedding_provider == "voyage" and settings.voyage_api_key:
            _embedder = VoyageEmbedder(
                settings.voyage_api_key, settings.voyage_model, settings.embedding_dim
            )
        else:
            _embedder = HashingEmbedder(settings.embedding_dim)
    return _embedder


def reset_embedder() -> None:
    global _embedder
    _embedder = None


def embed_text(text: str) -> list[float]:
    matrix = get_embedder().embed([text])
    return matrix[0].tolist() if len(matrix) else []


def embed_texts(texts: list[str]) -> list[list[float]]:
    return [row.tolist() for row in get_embedder().embed(texts)]


def cosine_similarity(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Rows of ``matrix`` are assumed L2-normalised; returns similarity in [-1, 1]."""
    if matrix.size == 0:
        return np.zeros(0, dtype=np.float32)
    norm = float(np.linalg.norm(query))
    if norm == 0:
        return np.zeros(matrix.shape[0], dtype=np.float32)
    return matrix @ (query / norm)
