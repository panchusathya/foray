from __future__ import annotations

import random
from dataclasses import replace

import pytest

from foray.rank import (
    BlendWeights,
    PoolRow,
    RankPolicy,
    Window,
    apply_ce_scores,
    build_pool,
    clamp_window,
    logistic_ce,
    needs_rerank,
    rank_page,
)


@pytest.fixture
def policy() -> RankPolicy:
    return RankPolicy(
        weights=BlendWeights(),
        display_floor=20,
        page_size=40,
        max_paginable=1000,
        min_results_before_widen=12,
        unscored_score=0,
        min_evidence=0,
        rerank_depth=48,
        rrf_k=60,
        n_arms=2,
    )


def test_absolute_ranks_on_second_page(policy: RankPolicy) -> None:
    rows = [PoolRow(i, 100 - i // 20, 0.5) for i in range(1, 101)]
    page = rank_page(rows, total_matched=100, window=Window(40, 40), policy=policy)
    assert page.items[0].rank == 41
    assert page.items[-1].rank == 80


def test_pages_partition_the_total_order(policy: RankPolicy) -> None:
    rows = [PoolRow(i, 50 + i % 30, (i % 17) / 17) for i in range(1, 181)]
    whole = rank_page(rows, total_matched=180, window=Window(0, 180), policy=policy)
    paged = []
    for offset in range(0, 180, 40):
        paged.extend(
            rank_page(rows, total_matched=180, window=Window(offset, 40), policy=policy).items
        )
    assert paged == whole.items
    assert len({item.key for item in paged}) == 180


def test_ties_are_deterministic_under_permutation(policy: RankPolicy) -> None:
    rows = [PoolRow(i, 60, 0.5) for i in range(1, 101)]
    shuffled = rows.copy()
    random.Random(42).shuffle(shuffled)
    expected = rank_page(rows, total_matched=100, window=Window(40, 40), policy=policy)
    actual = rank_page(shuffled, total_matched=100, window=Window(40, 40), policy=policy)
    assert actual == expected


def test_floor_vetoes_scored_rows_only(policy: RankPolicy) -> None:
    """Rewritten from test_floor_is_defensive: unscored rows are retrievable.

    A scored-19 is still excluded (the rubric looked and said no). A row with
    no score is not vetoed. min_evidence is 0 in this fixture so evidence is
    not confounding the floor.
    """
    page = rank_page(
        [PoolRow(1, 19, 1.0), PoolRow(2, 20, 0.0), PoolRow(3, None, 0.9)],
        total_matched=3,
        window=Window(0, 40),
        policy=policy,
    )
    assert 1 not in [item.key for item in page.items]
    assert {item.key for item in page.items} == {2, 3}


def test_min_evidence_drops_thin_rows(policy: RankPolicy) -> None:
    strict = RankPolicy(
        weights=BlendWeights(),
        display_floor=20,
        page_size=40,
        max_paginable=1000,
        min_results_before_widen=12,
        min_evidence=2,
        n_arms=1,
    )
    page = rank_page(
        [
            PoolRow(1, 80, 0.9, highlight_count=1),
            PoolRow(2, 40, 0.1, highlight_count=2),
        ],
        total_matched=2,
        window=Window(0, 40),
        policy=strict,
    )
    assert [item.key for item in page.items] == [2]


def test_clamp_window(policy: RankPolicy) -> None:
    assert clamp_window(0, 0, policy) == Window(0, 40)
    assert clamp_window(0, 99_999, policy) == Window(0, 1000)
    assert clamp_window(900, 99_999, policy) == Window(900, 100)
    assert clamp_window(50_000, 40, policy) == Window(1000, 0)


def test_truncation(policy: RankPolicy) -> None:
    rows = [PoolRow(i, 50, 0.5) for i in range(1, 1001)]
    page = rank_page(rows, total_matched=3000, window=Window(960, 40), policy=policy)
    assert page.paginable == 1000
    assert page.page_count == 25
    assert page.truncated is True
    assert page.has_more is False


def test_short_fused_pool_is_the_end_of_the_set(policy: RankPolicy) -> None:
    """Replacement for test_presorted_short_page_is_the_end_of_the_set.

    The fused pool, not a SQL-sliced page, is what we page over. A pool of 5
    with a stale count of 100 must not keep has_more true.
    """
    rows = [PoolRow(i, 50, 0.5) for i in range(1, 6)]
    page = rank_page(rows, total_matched=100, window=Window(0, 40), policy=policy)
    assert page.paginable == 5
    assert page.has_more is False
    assert [item.rank for item in page.items] == [1, 2, 3, 4, 5]

    empty = rank_page(rows, total_matched=100, window=Window(40, 40), policy=policy)
    assert empty.items == []
    assert empty.paginable == 5
    assert empty.has_more is False


def test_vector_rank_is_derived_from_exact_cos_sim() -> None:
    """The relaxed_order defence: a mis-ordered vector arm is ignored."""
    enrich = {
        1: PoolRow(1, 50, 0.1),
        2: PoolRow(2, 50, 0.9),
        3: PoolRow(3, 50, 0.5),
    }
    pool = build_pool(
        vector_ids=[1, 3, 2],  # HNSW returned 1 first, but 2 is closer
        lexical_ids=[],
        enrich=enrich,
        rrf_k=60,
    )
    by_id = {row.key: row for row in pool}
    assert by_id[2].rrf > by_id[3].rrf > by_id[1].rrf


def test_band_invariant_prefix(policy: RankPolicy) -> None:
    """Every band-0 row precedes every band-1 row, even when the tail's
    retrieval blend exceeds a head row's reranked blend."""
    rows = [
        PoolRow(1, 90, 0.9, rrf=0.02, ce=0.10, band=0),
        PoolRow(2, 30, 0.1, rrf=0.001, ce=0.99, band=0),
        PoolRow(3, 99, 0.99, rrf=0.03, ce=None, band=1),
    ]
    page = rank_page(rows, total_matched=3, window=Window(0, 40), policy=policy)
    assert [item.key for item in page.items] == [2, 1, 3]
    assert [item.band for item in page.items] == [0, 0, 1]


def test_determinism_under_float_jitter(policy: RankPolicy) -> None:
    logits = {i: 0.5 + (i % 7) * 0.01 for i in range(1, 61)}
    pool = [PoolRow(i, 50, 0.5, rrf=1.0 / (60 + i)) for i in range(1, 61)]
    reranked = apply_ce_scores(pool, logits, policy=policy)
    page = rank_page(reranked, total_matched=60, window=Window(0, 40), policy=policy)
    jittered = {key: value + 1e-7 * (1 if key % 2 else -1) for key, value in logits.items()}
    reranked_j = apply_ce_scores(pool, jittered, policy=policy)
    page_j = rank_page(reranked_j, total_matched=60, window=Window(0, 40), policy=policy)
    assert [item.key for item in page.items] == [item.key for item in page_j.items]


def test_needs_rerank_skips_below_head(policy: RankPolicy) -> None:
    assert needs_rerank(Window(0, 40), policy) is True
    assert needs_rerank(Window(200, 40), policy) is False
    assert needs_rerank(Window(48, 40), policy) is False


def test_logistic_ce_is_overflow_safe() -> None:
    assert logistic_ce(100.0) == 1.0
    assert logistic_ce(-100.0) == 0.0
    assert logistic_ce(0.0) == 0.5


# ------------------------------------------------------ CE logit gap (band 2) --


def test_ce_gap_demotes_deep_outliers_below_band_1(policy: RankPolicy) -> None:
    """A head row 14 logits under the head's best is a confident relative
    rejection: it lands in band 2, after band 1, keeping ce for diagnostics."""
    gapped = replace(policy, ce_logit_gap=5.0)
    pool = [
        PoolRow(1, 90, 0.9, rrf=0.02),  # strong retrieval, CE outlier (junk)
        PoolRow(2, 30, 0.2, rrf=0.001),  # weak retrieval, CE head max
        PoolRow(3, 80, 0.8, rrf=0.015),  # not scored by the CE -> band 1
    ]
    reranked = apply_ce_scores(pool, {1: -12.0, 2: 2.0}, policy=gapped)
    by_id = {row.key: row for row in reranked}
    assert by_id[1].band == 2
    assert by_id[1].ce == pytest.approx(0.0, abs=1e-4)  # kept, not erased
    assert by_id[2].band == 0
    assert by_id[3].band == 1 and by_id[3].ce is None
    page = rank_page(reranked, total_matched=3, window=Window(0, 40), policy=gapped)
    assert [item.key for item in page.items] == [2, 3, 1]
    assert [item.band for item in page.items] == [0, 1, 2]


def test_ce_gap_is_relative_not_absolute(policy: RankPolicy) -> None:
    """The prod calibration case: every head probability is tiny (max
    p=0.0029, logit -5.8), yet rows within the gap of the best stay band 0.
    An absolute floor demoted this entire head; the gap must not."""
    gapped = replace(policy, ce_logit_gap=5.0)
    pool = [
        PoolRow(1, 68, 0.6, rrf=0.01),
        PoolRow(2, 55, 0.5, rrf=0.008),
        PoolRow(3, 90, 0.9, rrf=0.02),
    ]
    reranked = apply_ce_scores(pool, {1: -5.8, 2: -9.0, 3: -12.0}, policy=gapped)
    by_id = {row.key: row for row in reranked}
    assert by_id[1].band == 0  # the head max itself
    assert by_id[2].band == 0  # 3.2 logits under: within the gap
    assert by_id[3].band == 2  # 6.2 logits under: demoted


def test_band_2_orders_by_retrieval_blend_not_ce(policy: RankPolicy) -> None:
    """Deep-outlier ce values are noise to sort by. Row 2 has the higher ce
    but row 1 has the stronger retrieval blend; retrieval wins in band 2."""
    gapped = replace(policy, ce_logit_gap=5.0)
    pool = [
        PoolRow(1, 90, 0.9, rrf=0.02),
        PoolRow(2, 20, 0.1, rrf=0.001),
        PoolRow(3, 50, 0.5, rrf=0.01),
    ]
    reranked = apply_ce_scores(pool, {1: -13.0, 2: -12.0, 3: 0.0}, policy=gapped)
    by_id = {row.key: row for row in reranked}
    assert by_id[1].band == 2 and by_id[2].band == 2 and by_id[3].band == 0
    page = rank_page(reranked, total_matched=3, window=Window(0, 40), policy=gapped)
    assert [item.key for item in page.items] == [3, 1, 2]



def test_band_0_ce_ties_break_on_retrieval_blend_not_cosine(policy: RankPolicy) -> None:
    """The prod flatline case: logistic_ce rounds to 4dp, so a head of tiny
    CE probabilities all ties at 0.0000. The tie must fall to the retrieval
    blend (rubric-scored rows first), not to cosine — cosine buried a
    score-72 candidate under an unscored semantically-adjacent one."""
    gapped = replace(policy, ce_logit_gap=5.0, unscored_score=20)
    pool = [
        PoolRow(1, 72, 0.55, rrf=0.01),   # rubric-scored, lower cosine
        PoolRow(2, None, 0.80, rrf=0.01),  # unscored, higher cosine
    ]
    # -10.0 and -10.1 both round to ce=0.0 and sit within the gap of each
    # other (max is -10.0, gap 5), so both stay band 0 and tie on ce.
    reranked = apply_ce_scores(pool, {1: -10.1, 2: -10.0}, policy=gapped)
    assert all(row.band == 0 for row in reranked)
    assert {row.ce for row in reranked} == {0.0}
    page = rank_page(reranked, total_matched=2, window=Window(0, 40), policy=gapped)
    assert [item.key for item in page.items] == [1, 2]
