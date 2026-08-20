"""Deterministic hashing embedder for tests and CI (no torch, no network)."""

from __future__ import annotations

import hashlib
import math
import struct


def hashing_embed(text: str, dim: int = 384) -> list[float]:
    """A stable unit vector from the query/document text.

    Not a retrieval model — it exists so ``run_search`` can be exercised in CI
    without sentence-transformers. Same text always yields the same vector.
    """
    seed = hashlib.sha256((text or "").encode()).digest()
    values: list[float] = []
    block = seed
    while len(values) < dim:
        for offset in range(0, len(block), 4):
            (raw,) = struct.unpack_from(">I", block, offset)
            values.append((raw / 4294967295.0) * 2.0 - 1.0)
            if len(values) >= dim:
                break
        block = hashlib.sha256(block).digest()
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values[:dim]]
