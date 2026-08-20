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
    blend_one,
    build_pool,
    clamp_window,
    logistic_ce,
    needs_rerank,
    normalize_rrf,
    page_count,
    rank_page,
    reciprocal_rank_fusion,
    widen_message,
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


def test_weights_of_zero_reproduce_legacy_blend() -> None:
    """Regression anchor: w_rrf=0, w_score=0.7, w_vector=0.3 is the old blend."""
    legacy = BlendWeights(rrf_weight=0.0, score_weight=0.7, vector_weight=0.3)
    assert blend_one(100, 0.0, legacy) == pytest.approx(0.7)
    assert blend_one(0, 1.0, legacy) == pytest.approx(0.3)
    assert blend_one(None, 0.83, legacy, unscored_score=0) == pytest.approx(0.0 * 0.7 + 0.3 * 0.83)


def test_vector_blend_can_reorder_archetype_scores(policy: RankPolicy) -> None:
    policy = RankPolicy(
        weights=BlendWeights(rrf_weight=0.0, score_weight=0.7, vector_weight=0.3),
        display_floor=20,
        page_size=40,
        max_paginable=1000,
        min_results_before_widen=12,
        min_evidence=0,
        n_arms=1,
    )
    page = rank_page(
        [PoolRow(1, 70, 0.1), PoolRow(2, 60, 0.9)],
        total_matched=2,
        window=Window(0, 40),
        policy=policy,
    )
    assert [item.key for item in page.items] == [2, 1]


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


@pytest.mark.parametrize(
    ("total", "expected"),
    [(160, 4), (180, 5), (200, 5), (201, 6), (0, 0), (1, 1)],
)
def test_page_count(total: int, expected: int) -> None:
    assert page_count(total, 40) == expected


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


def test_rank_page_preserves_input_identity_order_via_id_tiebreak(policy: RankPolicy) -> None:
    """Replacement for test_page_presorted_preserves_sql_order: equal blends
    sort by candidate id, not input order, so RRF ties are deterministic."""
    page = rank_page(
        [PoolRow(9, None, 0.4), PoolRow(2, None, 0.4)],
        total_matched=2,
        window=Window(0, 40),
        policy=policy,
    )
    assert [item.key for item in page.items] == [2, 9]


def test_widen_message_is_offset_invariant(policy: RankPolicy) -> None:
    first = rank_page(
        [PoolRow(i, 50, 0.5) for i in range(1, 46)],
        total_matched=45,
        window=Window(0, 40),
        policy=policy,
    )
    last = rank_page(
        [PoolRow(i, 50, 0.5) for i in range(1, 46)],
        total_matched=45,
        window=Window(40, 40),
        policy=policy,
    )
    assert widen_message(
        total_matched=first.total_matched,
        paginable=first.paginable,
        archetype_label="Backend",
        policy=policy,
    ) == widen_message(
        total_matched=last.total_matched,
        paginable=last.paginable,
        archetype_label="Backend",
        policy=policy,
    )


def test_page_envelope_is_counted_in_page_size_units(policy: RankPolicy) -> None:
    rows = [PoolRow(i, 50, 0.5) for i in range(1, 161)]
    select_all = rank_page(
        rows, total_matched=160, window=clamp_window(0, 160, policy), policy=policy
    )
    one_page = rank_page(rows, total_matched=160, window=Window(0, 40), policy=policy)

    assert len(select_all.items) == 160
    assert select_all.page == 1
    assert select_all.page_count == one_page.page_count == 4
    assert select_all.has_more is False

    middle = rank_page(rows, total_matched=160, window=Window(50, 40), policy=policy)
    assert middle.page == 2
    assert middle.page_count == 4


def test_page_never_exceeds_page_count(policy: RankPolicy) -> None:
    rows = [PoolRow(i, 50, 0.5) for i in range(1, 1001)]
    over = rank_page(
        rows, total_matched=1000, window=clamp_window(1200, 40, policy), policy=policy
    )
    assert over.items == []
    assert over.page_count == 25
    assert over.page <= over.page_count
    assert over.has_more is False

    empty = rank_page(rows[:0], total_matched=0, window=Window(0, 40), policy=policy)
    assert empty.page_count == 0
    assert empty.page == 1


def test_rrf_theoretical_max_is_batch_independent() -> None:
    raw = 2.0 / 61.0  # both arms rank 1, k=60
    assert normalize_rrf(raw, k=60, n_arms=2) == pytest.approx(1.0)
    lonely = 1.0 / 61.0
    assert normalize_rrf(lonely, k=60, n_arms=2) == pytest.approx(0.5)
    # Adding unrelated documents does not change the normalised value.
    assert normalize_rrf(raw, k=60, n_arms=2) == normalize_rrf(raw, k=60, n_arms=2)


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


def test_reciprocal_rank_fusion_k60() -> None:
    fused = reciprocal_rank_fusion([{1: 1, 2: 2}, {2: 1, 3: 2}], k=60)
    assert fused[2] == pytest.approx(1 / 61 + 1 / 62)
    assert fused[1] == pytest.approx(1 / 61)


# ----------------------------------------------------------- CE floor (band 2) --


def test_ce_floor_demotes_confident_irrelevance_below_band_1(policy: RankPolicy) -> None:
    """A head row the CE scored at p~=0.0025 (logit -6) must not stay pinned
    in band 0 above unreranked rows: it lands in band 2, after band 1, and
    keeps its ce for diagnostics."""
    floored = replace(policy, ce_floor=0.05)
    pool = [
        PoolRow(1, 90, 0.9, rrf=0.02),  # strong retrieval, CE says irrelevant
        PoolRow(2, 30, 0.2, rrf=0.001),  # weak retrieval, CE says relevant
        PoolRow(3, 80, 0.8, rrf=0.015),  # not scored by the CE -> band 1
    ]
    reranked = apply_ce_scores(pool, {1: -6.0, 2: 2.0}, policy=floored)
    by_id = {row.key: row for row in reranked}
    assert by_id[1].band == 2
    assert by_id[1].ce == pytest.approx(0.0025, abs=1e-4)  # kept, not erased
    assert by_id[2].band == 0
    assert by_id[3].band == 1 and by_id[3].ce is None
    page = rank_page(reranked, total_matched=3, window=Window(0, 40), policy=floored)
    assert [item.key for item in page.items] == [2, 3, 1]
    assert [item.band for item in page.items] == [0, 1, 2]


def test_band_2_orders_by_retrieval_blend_not_ce(policy: RankPolicy) -> None:
    """Sub-floor ce values are all ~0 and meaningless to sort by. Row 2 has
    the higher ce but row 1 has the stronger retrieval blend; retrieval wins."""
    floored = replace(policy, ce_floor=0.05)
    pool = [
        PoolRow(1, 90, 0.9, rrf=0.02),
        PoolRow(2, 20, 0.1, rrf=0.001),
    ]
    reranked = apply_ce_scores(pool, {1: -8.0, 2: -7.0}, policy=floored)
    assert all(row.band == 2 for row in reranked)
    ce_by_id = {row.key: row.ce for row in reranked}
    assert ce_by_id[2] > ce_by_id[1]  # ce alone would invert the order
    page = rank_page(reranked, total_matched=2, window=Window(0, 40), policy=floored)
    assert [item.key for item in page.items] == [1, 2]


def test_ce_at_floor_stays_band_0(policy: RankPolicy) -> None:
    """The floor is exclusive: ce == floor is a keep, not a demotion."""
    floored = replace(policy, ce_floor=0.5)
    reranked = apply_ce_scores([PoolRow(1, 50, 0.5)], {1: 0.0}, policy=floored)
    assert reranked[0].ce == 0.5
    assert reranked[0].band == 0


def test_ce_floor_zero_preserves_legacy_banding(policy: RankPolicy) -> None:
    """Library default ce_floor=0.0: even a hard CE rejection stays band 0,
    exactly the pre-floor behaviour (0.0 >= 0.0)."""
    assert policy.ce_floor == 0.0
    pool = [PoolRow(1, 90, 0.9, rrf=0.02), PoolRow(2, 30, 0.2, rrf=0.001)]
    reranked = apply_ce_scores(pool, {1: -30.0, 2: 3.0}, policy=policy)
    assert [row.band for row in reranked] == [0, 0]
    page = rank_page(reranked, total_matched=2, window=Window(0, 40), policy=policy)
    # Band 0 still orders by ce, so the rejected row sinks within the band
    # but is not ejected from it.
    assert [item.key for item in page.items] == [2, 1]
