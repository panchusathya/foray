"""Reciprocal Rank Fusion for combining lexical and vector result lists.

RRF is used instead of a weighted sum of raw scores because BM25 and cosine
similarity live on incomparable scales; ranks are the only thing they share.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import TypeVar

import numpy as np

K = TypeVar("K", bound=Hashable)


def rank_positions(scores: np.ndarray, mask: np.ndarray | None = None) -> dict[int, int]:
    """Map document index -> 1-based rank, best first. Masked-out docs are skipped."""
    if scores.size == 0:
        return {}
    order = np.argsort(-scores, kind="stable")
    positions: dict[int, int] = {}
    rank = 0
    for index in order:
        idx = int(index)
        if mask is not None and not mask[idx]:
            continue
        if scores[idx] <= 0:
            continue
        rank += 1
        positions[idx] = rank
    return positions


def reciprocal_rank_fusion(
    rankings: list[dict[K, int]],
    *,
    weights: list[float] | None = None,
    k: int = 60,
) -> dict[K, float]:
    """Standard RRF: score(d) = sum_i w_i / (k + rank_i(d))."""
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights and rankings must be the same length")

    fused: dict[K, float] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        for doc_index, rank in ranking.items():
            fused[doc_index] = fused.get(doc_index, 0.0) + weight / (k + rank)
    return fused


def normalize_scores(values: dict[K, float]) -> dict[K, float]:
    """Min-max the fused scores into 0..1 so they can be blended with others."""
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi - lo < 1e-12:
        return {key: 1.0 for key in values}
    return {key: (value - lo) / (hi - lo) for key, value in values.items()}
