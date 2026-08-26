from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import pytest

from foray.api.parse import llm_parse, parse_query
from foray.config import settings


@dataclass
class FakeChat:
    responses: list[str] = field(default_factory=list)
    raises: Exception | None = None
    calls: list[dict] = field(default_factory=list)

    def complete(
        self, system: str, user: str, *, model: str, max_tokens: int = 1600
    ) -> str:
        self.calls.append(
            {"system": system, "user": user, "model": model, "max_tokens": max_tokens}
        )
        if self.raises is not None:
            raise self.raises
        return self.responses.pop(0)


class StubConn:
    def __init__(self, cached=None) -> None:
        self.cached = cached
        self.executed = []

    async def fetchval(self, *_args):
        return self.cached

    async def execute(self, *args):
        self.executed.append(args)


def response(**overrides) -> str:
    payload = {
        "archetype_id": "senior_backend",
        "metros": ["San Francisco"],
        "languages": ["Go"],
        "experience_min": 5,
        "experience_max": None,
        "free_text": "distributed storage",
        **overrides,
    }
    return json.dumps(payload)


def test_fake_chat_parses_json_and_rejects_unknown_archetype() -> None:
    valid = FakeChat([response()])
    parsed = llm_parse("backend engineer", valid)
    assert parsed is not None
    assert parsed.archetype_id == "senior_backend"
    assert parsed.languages == ["go"]

    unknown = FakeChat([response(archetype_id="not-real")])
    parsed = llm_parse("backend engineer", unknown)
    assert parsed is not None and parsed.archetype_id is None


def test_fenced_json_is_accepted() -> None:
    fake = FakeChat([f"```json\n{response()}\n```"])
    parsed = llm_parse("backend engineer", fake)
    assert parsed is not None and parsed.archetype_id == "senior_backend"


@pytest.mark.asyncio
async def test_failure_degrades_to_heuristic_parser() -> None:
    fake = FakeChat(raises=RuntimeError("offline"))
    parsed, parser = await parse_query(
        StubConn(), "senior backend engineer in Go", use_llm=True, chat=fake
    )
    assert parser == "heuristic"
    assert parsed.archetype_id == "senior_backend"


@pytest.mark.asyncio
async def test_cache_hit_never_consults_chat_adapter() -> None:
    cached = response()
    fake = FakeChat(raises=AssertionError("chat must not run on a cache hit"))
    parsed, parser = await parse_query(
        StubConn(cached), "cached query", use_llm=True, chat=fake
    )
    assert parser == "cache"
    assert parsed.archetype_id == "senior_backend"
    assert fake.calls == []


def test_parse_model_is_threaded_to_adapter() -> None:
    fake = FakeChat([response()])
    assert llm_parse("backend engineer", fake) is not None
    assert fake.calls[0]["model"] == settings.parse_model


class TestTheParseNeverBlocksTheEventLoop:
    """The incident shape: ``complete`` is a synchronous httpx call inside a
    synchronous tenacity retry. Awaited inline it stops the whole uvicorn
    worker -- every other search, /search/stream, /health -- for as long as
    OpenRouter takes, while still holding a pooled asyncpg connection.
    """

    async def test_the_blocking_call_runs_off_the_loop(self) -> None:
        @dataclass
        class LoopSpy(FakeChat):
            saw_running_loop: bool = True

            def complete(self, system, user, *, model, max_tokens=1600):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    self.saw_running_loop = False
                return super().complete(system, user, model=model, max_tokens=max_tokens)

        spy = LoopSpy([response()])
        parsed, parser = await parse_query(StubConn(), "backend", use_llm=True, chat=spy)
        assert parser == "llm" and parsed.archetype_id == "senior_backend"
        assert spy.saw_running_loop is False

    async def test_the_loop_keeps_running_while_the_parse_blocks(self) -> None:
        """Not just "on another thread" -- other coroutines must actually make
        progress. A thread offload that still awaited serially would pass the
        test above and fail this one."""
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        beat = asyncio.ensure_future(heartbeat())
        try:
            slow = FakeChat([response()])
            slow.complete = _blocking_for(0.15, response())  # type: ignore[method-assign]
            await parse_query(StubConn(), "backend", use_llm=True, chat=slow)
        finally:
            beat.cancel()
        assert ticks > 1

    async def test_a_parse_over_the_deadline_degrades_instead_of_holding_the_request(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "parse_timeout_s", 0.05)
        stuck = FakeChat()
        stuck.complete = _blocking_for(0.4, response())  # type: ignore[method-assign]
        started = time.monotonic()
        parsed, parser = await parse_query(
            StubConn(), "senior backend engineer in Go", use_llm=True, chat=stuck
        )
        assert time.monotonic() - started < 0.3
        assert parser == "heuristic"
        assert parsed.archetype_id == "senior_backend"


def test_the_request_path_client_does_not_carry_the_batch_retry_stack() -> None:
    """Five attempts with exponential backoff is ~8 minutes of time.sleep. That
    is the scoring fan-out's trade, not a search request's."""
    from foray.api.parse import _request_path_client

    client = _request_path_client()
    assert client._attempts == 1
    assert client._timeout == settings.parse_timeout_s


class TestAFailedParseIsNotCached:
    """query_parse_cache rows have no TTL and are keyed on the query, so a
    cached fallback is permanent: one 429 would mean that query is never sent
    to the model again."""

    async def test_a_transient_failure_writes_no_cache_row(self) -> None:
        conn = StubConn()
        _parsed, parser = await parse_query(
            conn, "senior backend engineer", use_llm=True, chat=FakeChat(raises=RuntimeError("429"))
        )
        assert parser == "heuristic"
        assert conn.executed == []

    async def test_a_deliberate_heuristic_parse_is_still_cached(self) -> None:
        """No API key is a decision, not a blip -- caching it costs nothing and
        keeps the pre-existing behaviour."""
        conn = StubConn()
        await parse_query(conn, "senior backend engineer", use_llm=False)
        assert [a for a in conn.executed if "query_parse_cache" in a[0]]


def _blocking_for(seconds: float, payload: str):
    def complete(system, user, *, model, max_tokens=1600):
        time.sleep(seconds)
        return payload

    return complete
