"""Pure ranking and pagination policy for the Foray query path.

The database retrieves two bounded arms (vector + lexical), this module fuses
them with reciprocal rank fusion, then blends RRF with the precomputed
archetype score and exact cosine. Consequently the pageable results are the
blended order *within that fused pool*, not a claim that we computed a global
ranking over every eligible candidate.

This module must stay dependency-free: no settings, database, HTTP, or embedding
imports.  Keeping the arithmetic here makes page boundaries and total ordering
cheap to test without Postgres or the local embedding model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import ceil, exp


@dataclass(frozen=True)
class BlendWeights:
    rrf_weight: float = 0.5
    score_weight: float = 0.3
    vector_weight: float = 0.2


@dataclass(frozen=True)
class RankPolicy:
    weights: BlendWeights
    display_floor: int
    page_size: int
    max_paginable: int
    min_results_before_widen: int
    unscored_score: int = 0
    min_evidence: int = 2
    rerank_depth: int = 48
    # CE demotion gap, in LOGITS below the head's best row. A head row whose
    # raw CE logit sits more than this far under the head maximum is demoted
    # to band 2 (after every unreranked row) instead of staying pinned in
    # band 0 — see ``apply_ce_scores``. A gap of logits is an odds RATIO, so
    # it survives the corpus-level calibration shift that killed the first
    # design (an absolute probability floor): measured on prod, ms-marco
    # MiniLM tops out at p=0.0029 (logit -5.8) on candidate-profile documents
    # — every head row sat under any sane absolute floor while the incident
    # junk was still 6+ logits under the best row. The library default of
    # +inf disables demotion so dependency-free callers and CI keep the
    # pre-gap behaviour; the API wires ``settings.rerank_ce_gap`` in (this
    # module must stay settings-free).
    ce_logit_gap: float = float("inf")
    rrf_k: int = 60
    n_arms: int = 2


@dataclass(frozen=True)
class PoolRow:
    key: int
    score: int | None
    cos_sim: float
    rrf: float = 0.0
    highlight_count: int = 2
    ce: float | None = None
    band: int = 1


@dataclass(frozen=True)
class Ranked:
    key: int
    blend: float
    rank: int
    band: int = 1


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


def reciprocal_rank_fusion(
    rankings: Sequence[Mapping[int, int]],
    *,
    k: int = 60,
) -> dict[int, float]:
    """Standard RRF: score(d) = sum_i 1 / (k + rank_i(d)).

    RRF is used instead of a weighted sum of raw scores because lexical rank
    and cosine similarity live on incomparable scales; ranks are the only
    thing they share. ``k=60`` is the canonical constant (Cormack et al.).
    """
    fused: dict[int, float] = {}
    for ranking in rankings:
        for doc_id, rank in ranking.items():
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return fused


def normalize_rrf(raw: float, *, k: int, n_arms: int) -> float:
    """Absolute normalisation: divide by the theoretical maximum.

    A document ranked 1 by every arm scores ``n_arms / (k + 1)``. Dividing by
    that constant is batch-independent; min-maxing the fused list would make a
    row's score depend on who else happened to be retrieved, which breaks
    pagination.
    """
    if n_arms <= 0:
        return 0.0
    theoretical_max = n_arms / (k + 1)
    if theoretical_max <= 0:
        return 0.0
    return min(1.0, max(0.0, raw / theoretical_max))


def logistic_ce(logit: float) -> float:
    """Overflow-safe logistic, rounded to 4dp.

    ms-marco cross-encoders are BCE-trained, so the logit is a roughly
    calibrated relevance log-odds. Rounding absorbs Torch CPU non-bit-
    reproducibility across batch compositions; exact ties then fall through
    to cosine and candidate id.
    """
    if logit >= 20.0:
        value = 1.0
    elif logit <= -20.0:
        value = 0.0
    else:
        value = 1.0 / (1.0 + exp(-logit))
    return round(value, 4)


def blend_one(
    score: int | None,
    cos_sim: float,
    weights: BlendWeights,
    *,
    rrf: float = 0.0,
    unscored_score: int = 0,
    rrf_k: int = 60,
    n_arms: int = 2,
    ce: float | None = None,
) -> float:
    prior = float(score if score is not None else unscored_score)
    rrf_norm = normalize_rrf(rrf, k=rrf_k, n_arms=n_arms)
    retrieval = (
        weights.rrf_weight * rrf_norm
        + weights.score_weight * (prior / 100.0)
        + weights.vector_weight * cos_sim
    )
    if ce is None:
        return retrieval
    # Band-0 rows sort by the CE probability; the retrieval blend is retained
    # on Ranked.blend for diagnostics but does not order the head.
    return ce


def sort_key(blend: float, key: int, *, band: int = 1, cos_sim=0.0) -> tuple:
    """A total order: band 0 (reranked) first, then band 1 (unreranked), then
    band 2 (CE-rejected); within a band, higher blends, then the tiebreak,
    then id. ``cos_sim`` is the descending tiebreak — a float, or a tuple
    compared elementwise (rank_page passes (retrieval_blend, cos_sim) so a
    flat-lined CE falls back to the scored blend before cosine)."""
    values = cos_sim if isinstance(cos_sim, tuple) else (cos_sim,)
    return (band, -blend, tuple(-value for value in values), key)


def clamp_window(offset: int, limit: int, policy: RankPolicy) -> Window:
    safe_offset = max(0, min(offset, policy.max_paginable))
    requested = policy.page_size if limit <= 0 else limit
    remaining = max(0, policy.max_paginable - safe_offset)
    return Window(offset=safe_offset, limit=max(0, min(requested, remaining)))


def page_count(paginable: int, page_size: int) -> int:
    if paginable <= 0:
        return 0
    return ceil(paginable / page_size)


def needs_rerank(window: Window, policy: RankPolicy) -> bool:
    """Skip CE inference when the requested window sits entirely below the head.

    Band-1 ranks depend only on |band 0|, which is ``rerank_depth`` (or the
    fused-pool size if smaller) and is known without running the model.
    """
    return window.offset < policy.rerank_depth and window.limit > 0


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


def vector_ranks_from_cos(rows: Iterable[PoolRow]) -> dict[int, int]:
    """Re-derive vector ranks from exact cosine, ignoring HNSW order.

    ``hnsw.iterative_scan = relaxed_order`` is explicitly allowed to return
    approximate ordering. Approximate index order must not leak into RRF.
    """
    ordered = sorted(rows, key=lambda row: (-row.cos_sim, row.key))
    return {row.key: index for index, row in enumerate(ordered, start=1)}


def build_pool(
    *,
    vector_ids: Sequence[int],
    lexical_ids: Sequence[int],
    score_ids: Sequence[int] | None = None,
    enrich: Mapping[int, PoolRow],
    rrf_k: int = 60,
) -> list[PoolRow]:
    """Fuse arm id-lists with RRF. Vector ranks come from exact cosine in enrich."""
    present = {
        key: row
        for key, row in enrich.items()
        if key in set(vector_ids) | set(lexical_ids) | set(score_ids or ())
    }
    rankings: list[dict[int, int]] = []
    vector_present = [row for row in present.values() if row.key in set(vector_ids)]
    if vector_present:
        rankings.append(vector_ranks_from_cos(vector_present))
    if lexical_ids:
        rankings.append(
            {key: index for index, key in enumerate(lexical_ids, start=1) if key in present}
        )
    if score_ids:
        rankings.append(
            {key: index for index, key in enumerate(score_ids, start=1) if key in present}
        )
    fused = reciprocal_rank_fusion(rankings, k=rrf_k)
    pool: list[PoolRow] = []
    for key, row in present.items():
        pool.append(
            PoolRow(
                key=key,
                score=row.score,
                cos_sim=row.cos_sim,
                rrf=fused.get(key, 0.0),
                highlight_count=row.highlight_count,
                ce=row.ce,
                band=row.band,
            )
        )
    return pool


def apply_ce_scores(
    pool: Sequence[PoolRow],
    logits: Mapping[int, float],
    *,
    policy: RankPolicy,
) -> list[PoolRow]:
    """Attach squashed CE scores to the retrieval-ordered prefix; band the rest.

    Do not impute a CE score to the tail: zero is a false "rejected" claim,
    and a batch mean reintroduces batch dependence.

    Head rows whose raw logit sits more than ``policy.ce_logit_gap`` under
    the head's best logit land in band 2, not band 0. Without demotion,
    band 0 could reorder the head but never eject from it: a row the CE
    scored at logit -12 still outranked every band-1 row, including score-88
    candidates. The gap is relative on purpose — see the RankPolicy field
    for the prod calibration measurements that ruled out an absolute floor.
    Consequence worth knowing: a head that is UNIFORMLY junk has no relative
    signal, so nothing demotes and the CE merely reorders it — retrieval
    already put junk on top and the CE cannot repair that. The demotion
    keeps the row paginable (the CE is a small local model, so "irrelevant"
    is a demote, not a hide) and keeps ``ce`` on the row for diagnostics.
    """
    depth = policy.rerank_depth
    retrieval_ordered = sorted(
        pool,
        key=lambda row: sort_key(
            blend_one(
                row.score,
                row.cos_sim,
                policy.weights,
                rrf=row.rrf,
                unscored_score=policy.unscored_score,
                rrf_k=policy.rrf_k,
                n_arms=policy.n_arms,
            ),
            row.key,
            band=1,
            cos_sim=row.cos_sim,
        ),
    )
    head_keys = {row.key for row in retrieval_ordered[:depth]}
    scored_keys = [row.key for row in retrieval_ordered[:depth] if row.key in logits]
    # The gap compares RAW logits: logistic_ce rounds to 4dp, which collapses
    # the sub-0.0001 region the demotion decision lives in on this corpus.
    max_logit = max((logits[key] for key in scored_keys), default=0.0)
    out: list[PoolRow] = []
    for row in pool:
        if row.key in logits and row.key in head_keys:
            ce = logistic_ce(logits[row.key])
            # Within the gap of the head max: band 0, ordered by ce in
            # rank_page. Further below: band 2 — ce is kept for diagnostics,
            # but that far under the best row it is noise to sort by, so
            # rank_page orders band 2 by the retrieval blend instead.
            band = 0 if (max_logit - logits[row.key]) <= policy.ce_logit_gap else 2
            out.append(
                PoolRow(
                    key=row.key,
                    score=row.score,
                    cos_sim=row.cos_sim,
                    rrf=row.rrf,
                    highlight_count=row.highlight_count,
                    ce=ce,
                    band=band,
                )
            )
        else:
            out.append(
                PoolRow(
                    key=row.key,
                    score=row.score,
                    cos_sim=row.cos_sim,
                    rrf=row.rrf,
                    highlight_count=row.highlight_count,
                    ce=None,
                    band=1,
                )
            )
    return out


def rank_page(
    pool: Iterable[PoolRow],
    *,
    total_matched: int,
    window: Window,
    policy: RankPolicy,
) -> Page:
    blended: list[tuple[float, PoolRow]] = []
    for row in pool:
        if row.highlight_count < policy.min_evidence:
            continue
        # display_floor is a veto on scored rows only. A candidate the rubric
        # scored below the floor is excluded — the rubric looked and said no.
        # A candidate with no score is not vetoed; there is no information to
        # floor. This is the inverse of the pre-hybrid gate
        # ``score is not None and score >= floor``.
        if row.score is not None and row.score < policy.display_floor:
            continue
        retrieval = blend_one(
            row.score,
            row.cos_sim,
            policy.weights,
            rrf=row.rrf,
            unscored_score=policy.unscored_score,
            rrf_k=policy.rrf_k,
            n_arms=policy.n_arms,
        )
        # Only band 0 orders by ce. A band-2 (CE-rejected) row keeps its ce
        # for diagnostics, but that far under the head max it is noise, so
        # band 2 orders by the retrieval blend like band 1.
        blend = row.ce if (row.band == 0 and row.ce is not None) else retrieval
        blended.append((blend, retrieval, row))

    # Band 0 breaks ce ties on the RETRIEVAL BLEND, not cosine. logistic_ce
    # rounds to 4dp, and on this corpus (profile blobs, not the passages
    # ms-marco was trained on) most of a head ties at 0.0000 — a cosine
    # tiebreak then buried rubric-scored rows under semantically-adjacent
    # unscored ones. Where the CE genuinely discriminates it still leads;
    # where it flatlines, the scored blend decides. Bands 1/2 sort by the
    # retrieval blend itself, so the same tiebreak is a no-op for them and
    # cosine still settles exact blend ties via sort_key.
    blended.sort(
        key=lambda item: sort_key(
            item[0],
            item[2].key,
            band=item[2].band,
            cos_sim=(item[1], item[2].cos_sim),
        )
    )

    # The count and pool queries run under a snapshot, but hydrate is a later
    # statement. Bound the advertised window by the rows this request actually
    # observed so a concurrent update cannot create an empty advertised page.
    # len(blended) is now bounded by the arms' k rather than by a SQL LIMIT on
    # archetype score, and is the only thing preventing the pager from
    # advertising pages the fused pool cannot fill.
    paginable = min(total_matched, policy.max_paginable, len(blended))
    stop = min(window.offset + window.limit, paginable)
    items = [
        Ranked(key=row.key, blend=blend, rank=index + 1, band=row.band)
        for index, (blend, _retrieval, row) in enumerate(
            blended[window.offset : stop], start=window.offset
        )
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
    archetype_label: str | None,
    policy: RankPolicy,
    scored_matched: int | None = None,
    fused: int | None = None,
) -> str:
    """Copy that stays honest once unscored rows are retrievable.

    ``cleared the bar for {archetype}`` is only true of the scored subset.
    """
    label = archetype_label or "this search"
    if total_matched == 0:
        message = (
            f"Nothing matched {label}. Widen the search "
            "(drop a filter or lower the experience floor) to see more."
        )
    elif total_matched < policy.min_results_before_widen:
        scored_note = ""
        if scored_matched is not None:
            scored_note = f" ({scored_matched} scored at or above the bar)"
        message = (
            f"Only {total_matched} candidate(s) matched {label}{scored_note}. "
            "Widen the search (drop a filter or lower the experience floor) to see more."
        )
    else:
        scored_note = ""
        if scored_matched is not None:
            scored_note = f" {scored_matched} scored at or above the bar."
        message = f"{total_matched} candidates matched {label}.{scored_note}"

    shown = fused if fused is not None else paginable
    if total_matched > paginable:
        message += (
            f" Showing {paginable} ranked candidates from a fused retrieval pool "
            f"of {shown} — narrow the query to reach the rest."
        )
    return message
