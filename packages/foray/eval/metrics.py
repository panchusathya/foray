"""Copied from eval/run_eval.py — foray must not import juicebox."""

from __future__ import annotations

import math


def precision_at_k(labels: list[bool], k: int) -> float:
    window = labels[:k]
    return sum(window) / len(window) if window else 0.0


def recall_at_k(labels: list[bool], total_relevant: int, k: int) -> float:
    return sum(labels[:k]) / total_relevant if total_relevant else 0.0


def mrr(labels: list[bool]) -> float:
    for index, relevant in enumerate(labels, start=1):
        if relevant:
            return 1.0 / index
    return 0.0


def ndcg_at_k(labels: list[bool], total_relevant: int, k: int) -> float:
    dcg = sum(1.0 / math.log2(index + 1) for index, hit in enumerate(labels[:k], start=1) if hit)
    ideal_hits = min(total_relevant, k)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0
