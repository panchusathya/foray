from __future__ import annotations

import pytest

from foray.enrich.linkedin import resolve_linkedin


class RecordingFetcher:
    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []

    async def get_text(self, url: str) -> str | None:
        self.calls.append(url)
        return self.pages.get(url)


async def test_waterfall_stops_at_step_1_and_skips_network_and_apollo():
    apollo_called = False

    async def apollo(_node):
        nonlocal apollo_called
        apollo_called = True
        return "https://linkedin.com/in/should-not-happen"

    fetcher = RecordingFetcher()
    node = {"login": "ada", "bio": "ML. https://linkedin.com/in/ada", "websiteUrl": ""}
    url, source = await resolve_linkedin(
        node, fetcher=fetcher, allow_apollo=True, apollo_reason="shortlist", apollo=apollo
    )
    assert url == "https://linkedin.com/in/ada"
    assert source == "bio"
    assert fetcher.calls == []  # step 1 hit -> no README/website fetch
    assert apollo_called is False  # and no Apollo credit spent


async def test_apollo_requires_reason():
    fetcher = RecordingFetcher()
    node = {"login": "ghost", "bio": "", "websiteUrl": ""}
    with pytest.raises(ValueError):
        await resolve_linkedin(node, fetcher=fetcher, allow_apollo=True, apollo_reason=None)


async def test_apollo_not_called_without_allow_flag():
    called = False

    async def apollo(_node):
        nonlocal called
        called = True
        return "x"

    node = {"login": "ghost", "bio": "", "websiteUrl": ""}
    url, source = await resolve_linkedin(
        node, fetcher=RecordingFetcher(), allow_apollo=False, apollo=apollo
    )
    assert url is None and source is None
    assert called is False


# --------------------------------------------------------------- freshness --


