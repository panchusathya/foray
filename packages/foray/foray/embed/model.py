"""The local embedding model (BAAI/bge-small-en-v1.5, 384 dims, CPU).

The model version is pinned in a constant because changing the model invalidates
every stored vector. bge-small is asymmetric: queries get an instruction prefix,
documents (candidate blobs) do not.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from ..config import settings

if TYPE_CHECKING:
    import numpy as np

# Bump this (and re-embed everything) only when changing the model.
EMBEDDING_MODEL_VERSION = "bge-small-en-v1.5"

# bge retrieval instruction, applied to QUERIES only (not documents).
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=1)
def get_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(settings.embedding_model)


def embed_documents(texts: list[str]) -> "np.ndarray":
    return get_model().encode(
        texts, normalize_embeddings=True, batch_size=64, show_progress_bar=False
    )


def embed_query(text: str) -> "np.ndarray":
    return get_model().encode(
        [QUERY_INSTRUCTION + text], normalize_embeddings=True, show_progress_bar=False
    )[0]


def to_halfvec_literal(vector) -> str:
    """pgvector text literal, e.g. '[0.1,0.2,...]'. Works for vector and halfvec."""
    return "[" + ",".join(f"{float(x):.6f}" for x in vector) + "]"
