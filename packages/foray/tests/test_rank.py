from __future__ import annotations

import random

import pytest

from foray.rank import (
    BlendWeights,
    PoolRow,
    RankPolicy,
    Window,
    blend_one,
    clamp_window,
    page_count,
    page_presorted,
    rank_page,
    widen_message,
)


@pytest.fixture
def policy() -> RankPolicy:
    return RankPolicy(
        weights=BlendWeights(),
        display_floor=20,
        page_size=40,
        max_paginable=1000,
        retrieval_pool=300,
        min_results_before_widen=12,
    )


def test_blend_one_weights() -> None:
    weights = BlendWeights()
    assert blend_one(100, 0.0, weights) == pytest.approx(0.7)
    assert blend_one(0, 1.0, weights) == pytest.approx(0.3)
    assert blend_one(None, 0.83, weights) == pytest.approx(0.83)


def test_vector_blend_can_reorder_archetype_scores(policy: RankPolicy) -> None:
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


def test_floor_is_defensive(policy: RankPolicy) -> None:
    page = rank_page(
        [PoolRow(1, 19, 1.0), PoolRow(2, 20, 0.0)],
        total_matched=1,
        window=Window(0, 40),
        policy=policy,
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


def test_page_presorted_preserves_sql_order(policy: RankPolicy) -> None:
    page = page_presorted(
        [PoolRow(9, None, 0.4), PoolRow(2, None, 0.9)],
        total_matched=42,
        window=Window(40, 2),
        policy=policy,
    )
    assert [item.key for item in page.items] == [9, 2]
    assert [item.rank for item in page.items] == [41, 42]


def test_pool_size_covers_pageable_window(policy: RankPolicy) -> None:
    assert policy.pool_size == 1000


def test_page_envelope_is_counted_in_page_size_units(policy: RankPolicy) -> None:
    """A select-all request must not contradict the pager that issued it."""
    rows = [PoolRow(i, 50, 0.5) for i in range(1, 161)]
    select_all = rank_page(
        rows, total_matched=160, window=clamp_window(0, 160, policy), policy=policy
    )
    one_page = rank_page(rows, total_matched=160, window=Window(0, 40), policy=policy)

    assert len(select_all.items) == 160
    assert select_all.page == 1
    assert select_all.page_count == one_page.page_count == 4
    assert select_all.has_more is False

    # An offset that is not a multiple of page_size reports the page it is in.
    middle = rank_page(rows, total_matched=160, window=Window(50, 40), policy=policy)
    assert middle.page == 2
    assert middle.page_count == 4


def test_page_never_exceeds_page_count(policy: RankPolicy) -> None:
    """An API caller asking past the ceiling gets an empty page, not page 26 of 25."""
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


def test_presorted_short_page_is_the_end_of_the_set(policy: RankPolicy) -> None:
    """A count taken before the page query must not outlive the rows it counted.

    The fallback path counts and slices in two statements under READ COMMITTED.
    If rows vanish in between, the stale count must not keep `has_more` true and
    leave the pager offering a page that cannot be filled.
    """
    vanished = page_presorted(
        [], total_matched=100, window=Window(80, 40), policy=policy
    )
    assert vanished.paginable == 80
    assert vanished.has_more is False

    # A genuinely short last page behaves the same way, and is the common case.
    last = page_presorted(
        [PoolRow(i, None, 0.5) for i in range(1, 6)],
        total_matched=45,
        window=Window(40, 40),
        policy=policy,
    )
    assert last.paginable == 45
    assert last.has_more is False
    assert [item.rank for item in last.items] == [41, 42, 43, 44, 45]

    # A full page still trusts the count and keeps paging.
    full = page_presorted(
        [PoolRow(i, None, 0.5) for i in range(1, 41)],
        total_matched=100,
        window=Window(40, 40),
        policy=policy,
    )
    assert full.paginable == 100
    assert full.has_more is True
