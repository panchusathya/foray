from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

from foray.config import settings
from foray.rank import Window, needs_rerank
from foray.rerank.cache import TtlLru
from foray.rerank.document import build_document
from foray.rerank.runner import rerank, reset_for_tests


@dataclass
class FakeReranker:
    scores: list[float] = field(default_factory=list)
    raises: Exception | None = None
    delay_s: float = 0.0
    calls: int = 0

    def __call__(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.raises is not None:
            raise self.raises
        if self.scores:
            return list(self.scores)
        return [float(i) for i in range(len(pairs))]


@pytest.fixture(autouse=True)
def _reset_runner(monkeypatch):
    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(settings, "rerank_timeout_ms", 200)
    monkeypatch.setattr(settings, "rerank_queue_timeout_ms", 50)
    monkeypatch.setattr(settings, "rerank_failure_threshold", 2)
    monkeypatch.setattr(settings, "rerank_cache_entries", 128)
    monkeypatch.setattr(settings, "rerank_cache_ttl_s", 30)
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.mark.asyncio
async def test_fake_reranker_returns_logits() -> None:
    fake = FakeReranker(scores=[1.5, -0.2])
    outcome = await rerank("vlm", {1: "trained vlms", 2: "used clip"}, scorer=fake)
    assert outcome.reason == "ok"
    assert outcome.logits[1] == pytest.approx(1.5)
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_cache_hit_skips_scorer() -> None:
    fake = FakeReranker(scores=[2.0])
    first = await rerank("vlm", {1: "doc"}, scorer=fake)
    assert first.reason == "ok"
    fake.raises = AssertionError("scorer must not run on a cache hit")
    second = await rerank("vlm", {1: "doc"}, scorer=fake)
    assert second.logits[1] == pytest.approx(2.0)
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_timeout_degrades() -> None:
    fake = FakeReranker(delay_s=0.25)
    outcome = await rerank("vlm", {1: "doc"}, scorer=fake)
    assert outcome.reason == "timeout"
    assert outcome.logits == {}
    time.sleep(0.3)  # let the worker release the semaphore


@pytest.mark.asyncio
async def test_failure_degrades_then_opens_breaker() -> None:
    fake = FakeReranker(raises=RuntimeError("no model"))
    first = await rerank("vlm", {1: "doc"}, scorer=fake)
    second = await rerank("vlm", {2: "doc"}, scorer=fake)
    assert first.reason == "failed"
    assert second.reason == "failed"
    fake.raises = None
    fake.scores = [9.0]
    third = await rerank("vlm", {3: "doc"}, scorer=fake)
    # Circuit opened after threshold; subsequent calls skip inference.
    assert third.reason == "failed"
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_disabled_short_circuits(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rerank_enabled", False)
    fake = FakeReranker(raises=AssertionError("must not run"))
    outcome = await rerank("vlm", {1: "doc"}, scorer=fake)
    assert outcome.reason == "disabled"
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_queue_full_degrades() -> None:
    import foray.rerank.runner as runner

    sem = runner._get_semaphore()
    await sem.acquire()
    try:
        fake = FakeReranker()
        outcome = await rerank("vlm", {1: "doc"}, scorer=fake)
        assert outcome.reason == "queue_full"
        assert fake.calls == 0
    finally:
        sem.release()


def test_needs_rerank_offset_200_skips() -> None:
    from foray.rank import BlendWeights, RankPolicy

    policy = RankPolicy(
        weights=BlendWeights(),
        display_floor=20,
        page_size=40,
        max_paginable=1000,
        min_results_before_widen=12,
        rerank_depth=48,
    )
    assert needs_rerank(Window(200, 40), policy) is False


def test_linkedin_document_omits_stars() -> None:
    doc = build_document(
        {
            "evidence_kind": "linkedin",
            "employer": "Stripe",
            "title": "Staff Engineer",
            "est_experience_years": 7,
            "top_repos": [{"name": "x", "stars": 0}],
            "commits_last_year": 0,
            "domain_markers": ["ml"],
        }
    )
    assert "0★" not in doc
    assert "commits" not in doc
    assert "Stripe" in doc


def test_ttl_lru_expires() -> None:
    cache = TtlLru(maxsize=2, ttl_s=0.01)
    cache.put("a", 1.0)
    assert cache.get("a") == 1.0
    import time

    time.sleep(0.02)
    assert cache.get("a") is None
