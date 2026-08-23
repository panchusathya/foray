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


def junk_at_k(labels_junk: list[bool], k: int) -> float:
    """Fraction of the top-k results that are known junk (seeded distractors).

    Same arithmetic as precision_at_k but kept separate on purpose: the input
    is a DIFFERENT label stream (is-this-a-distractor, not is-this-relevant),
    and a result can legitimately be neither relevant nor junk — merging the
    two invites passing the wrong labels list.
    """
    window = labels_junk[:k]
    return sum(window) / len(window) if window else 0.0


def filter_violations_at_k(violations: list[bool], k: int) -> float:
    """Fraction of the top-k that VIOLATE a hard filter the query actually ran.

    A THIRD label stream, distinct from both of the above and not derivable from
    either. Relevance asks "should a human want this row?"; junk asks "is this a
    seeded off-domain persona?"; this asks "did the system return a row its own
    SQL predicate excluded?" A row can be irrelevant without violating anything
    (merely a bad ranking) and — the case that matters — a row can violate a
    stated filter while being perfectly relevant and not junk, which is why
    neither of the other two streams can substitute.

    The distinction is the whole point of a Boolean-first system: nDCG@10 can sit
    at 0.9 while a filter leaks entirely, because the leaked rows are drawn from
    the same population the relevance rule already likes. The prod incident read
    as a ranking problem for exactly this reason. Correct behaviour here is not
    "low" but ZERO — a violated hard filter is a bug, not a ranking preference,
    so run_eval gates at 0 rather than at a tolerance.

    Same arithmetic as precision_at_k / junk_at_k and kept separate for the same
    reason: passing the wrong labels list is the easy mistake, and it would be
    silent. Callers must pass False (not omit the entry) for a row they judged
    and found clean, so that len() is the window actually judged.
    """
    window = violations[:k]
    return sum(window) / len(window) if window else 0.0


def ndcg_at_k(labels: list[bool], total_relevant: int, k: int) -> float:
    dcg = sum(1.0 / math.log2(index + 1) for index, hit in enumerate(labels[:k], start=1) if hit)
    ideal_hits = min(total_relevant, k)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0
