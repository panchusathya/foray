from __future__ import annotations

import json
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
