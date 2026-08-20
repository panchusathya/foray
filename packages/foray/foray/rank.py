"""Pure ranking and pagination policy for the Foray query path.

The database deliberately retrieves a bounded pool ordered by archetype score,
then this module applies the score/vector blend.  Consequently the pageable
results are the blended order *within that retrieval pool*, not a claim that we
computed a global vector blend over every eligible candidate.

This module must stay dependency-free: no settings, database, HTTP, or embedding
imports.  Keeping the arithmetic here makes page boundaries and total ordering
cheap to test without Postgres or the local embedding model.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True)
class BlendWeights:
    score_weight: float = 0.7
    vector_weight: float = 0.3


@dataclass(frozen=True)
class RankPolicy:
    weights: BlendWeights
    display_floor: int
    page_size: int
    max_paginable: int
    retrieval_pool: int
    min_results_before_widen: int

    @property
    def pool_size(self) -> int:
        """Rows retrieved by archetype score before the vector blend."""
        return max(self.retrieval_pool, self.max_paginable)


@dataclass(frozen=True)
class PoolRow:
    key: int
    score: int | None
    cos_sim: float


@dataclass(frozen=True)
class Ranked:
    key: int
    blend: float
    rank: int


@dataclass(frozen=True)
class Window:
    offset: int
    limit: int


@dataclass(frozen=True)
class Page:
    items: list[Ranked]
    offset: int
    limit: int
    total_matched: int
    paginable: int
    page: int
    page_count: int
    has_more: bool
    truncated: bool


def blend_one(score: int | None, cos_sim: float, weights: BlendWeights) -> float:
    if score is None:
        return cos_sim
    return weights.score_weight * (score / 100.0) + weights.vector_weight * cos_sim


def sort_key(blend: float, key: int) -> tuple[float, int]:
    """A total order: higher blends first, then the stable candidate id."""
    return (-blend, key)


def clamp_window(offset: int, limit: int, policy: RankPolicy) -> Window:
    safe_offset = max(0, min(offset, policy.max_paginable))
    requested = policy.page_size if limit <= 0 else limit
    remaining = max(0, policy.max_paginable - safe_offset)
    return Window(offset=safe_offset, limit=max(0, min(requested, remaining)))


def page_count(paginable: int, page_size: int) -> int:
    if paginable <= 0:
        return 0
    return ceil(paginable / page_size)


def _envelope(
    items: list[Ranked],
    *,
    total_matched: int,
    paginable: int,
    window: Window,
    policy: RankPolicy,
) -> Page:
    """Describe a slice in whole pages.

    ``page`` and ``page_count`` are always expressed in ``policy.page_size``
    units, never in the caller's ``limit``. That keeps the numbers stable for
    the only consumer that pages: a select-all request (``limit`` == the whole
    paginable set, ``offset`` 0) reports page 1 of the same ``page_count`` the
    UI walks through, instead of collapsing the set to a single page and
    contradicting the pager it was issued from. An ``offset`` that is not a
    multiple of ``page_size`` is reported as the page it falls inside.

    ``page`` is capped at ``page_count``: an API caller asking for an offset
    past the ceiling gets an empty page rather than a page number that does not
    exist in the envelope it is reported alongside.
    """
    pages = page_count(paginable, policy.page_size)
    page_number = min(window.offset // policy.page_size + 1, pages) if pages else 1
    return Page(
        items=items,
        offset=window.offset,
        limit=window.limit,
        total_matched=total_matched,
        paginable=paginable,
        page=page_number,
        page_count=pages,
        has_more=window.offset + len(items) < paginable,
        truncated=total_matched > paginable,
    )


def rank_page(
    pool: Iterable[PoolRow],
    *,
    total_matched: int,
    window: Window,
    policy: RankPolicy,
) -> Page:
    blended = [
        (blend_one(row.score, row.cos_sim, policy.weights), row)
        for row in pool
        if row.score is not None and row.score >= policy.display_floor
    ]
    blended.sort(key=lambda item: sort_key(item[0], item[1].key))

    # The count and pool queries run under READ COMMITTED. Bound the advertised
    # window by the rows this request actually observed so a concurrent update
    # cannot create an empty advertised page.
    paginable = min(total_matched, policy.max_paginable, len(blended))
    stop = min(window.offset + window.limit, paginable)
    items = [
        Ranked(key=row.key, blend=blend, rank=index + 1)
        for index, (blend, row) in enumerate(blended[window.offset:stop], start=window.offset)
    ]
    return _envelope(
        items,
        total_matched=total_matched,
        paginable=paginable,
        window=window,
        policy=policy,
    )


def page_presorted(
    ordered: Iterable[PoolRow],
    *,
    total_matched: int,
    window: Window,
    policy: RankPolicy,
) -> Page:
    """Number an already ordered and SQL-sliced vector-fallback page."""
    rows = list(ordered)
    paginable = min(total_matched, policy.max_paginable)
    # Same READ COMMITTED defence as rank_page, expressed against a page SQL
    # has already sliced: a page that came back short of the window is the end
    # of the set, whatever the count query said a moment earlier. Without this,
    # rows deleted between the two statements leave `has_more` true and the
    # pager offering a page that cannot exist.
    if len(rows) < window.limit:
        paginable = min(paginable, window.offset + len(rows))
    items = [
        Ranked(
            key=row.key,
            blend=blend_one(row.score, row.cos_sim, policy.weights),
            rank=window.offset + index + 1,
        )
        for index, row in enumerate(rows)
    ]
    return _envelope(
        items,
        total_matched=total_matched,
        paginable=paginable,
        window=window,
        policy=policy,
    )


def widen_message(
    *,
    total_matched: int,
    paginable: int,
    archetype_label: str,
    policy: RankPolicy,
) -> str:
    if total_matched == 0:
        message = (
            f"Nothing cleared the bar for {archetype_label}. Widen the search "
            "(drop a filter or lower the experience floor) to see more."
        )
    elif total_matched < policy.min_results_before_widen:
        message = (
            f"Only {total_matched} candidate(s) cleared the bar for {archetype_label}. "
            "Widen the search (drop a filter or lower the experience floor) to see more."
        )
    else:
        message = f"{total_matched} candidates cleared the bar for {archetype_label}."

    if total_matched > paginable:
        message += (
            f" Showing {paginable} ranked candidates from the bounded retrieval pool — "
            "narrow the query to reach the rest."
        )
    return message
