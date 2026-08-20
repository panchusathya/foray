"""Exa HTTP client for job-posting search.

No key → ``ExaUnavailable``. The scrape handler records a skip rather than
failing the job, mirroring ``LLMUnavailable``.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from ..config import get_settings

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"


class ExaUnavailable(RuntimeError):
    """Raised when Exa is called with no usable backend."""


class ExaTransport(Protocol):
    def __call__(self, method: str, url: str, json: dict[str, Any]) -> dict[str, Any]: ...


class ExaClient:
    def __init__(self, api_key: str = "", transport: ExaTransport | None = None) -> None:
        self.api_key = api_key
        self.transport = transport

    @property
    def available(self) -> bool:
        return bool(self.api_key) or self.transport is not None

    def search_job_postings(self, query: str, *, num_results: int = 10) -> list[dict[str, Any]]:
        if not self.available:
            raise ExaUnavailable("EXA_API_KEY is not set")
        payload = {
            "query": query,
            "category": "job posting",
            "numResults": max(1, min(num_results, 25)),
            "contents": {"text": True},
        }
        data = self._post(EXA_SEARCH_URL, payload)
        results = list(data.get("results") or [])
        missing_ids = [r.get("id") for r in results if not (r.get("text") or r.get("summary"))]
        missing_ids = [i for i in missing_ids if i]
        if missing_ids:
            contents = self._post(EXA_CONTENTS_URL, {"ids": missing_ids, "text": True})
            by_id = {item.get("id"): item for item in contents.get("results") or []}
            for row in results:
                extra = by_id.get(row.get("id")) or {}
                if extra.get("text") and not row.get("text"):
                    row["text"] = extra["text"]
        return results

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.transport is not None:
            return self.transport("POST", url, payload)
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}
        response = httpx.post(url, headers=headers, json=payload, timeout=30.0)
        response.raise_for_status()
        return response.json()


_client: ExaClient | None = None


def get_exa() -> ExaClient:
    global _client
    if _client is None:
        _client = ExaClient(api_key=get_settings().exa_api_key)
    return _client


def set_exa(client: ExaClient | None) -> None:
    global _client
    _client = client


def reset_exa() -> None:
    global _client
    _client = None
