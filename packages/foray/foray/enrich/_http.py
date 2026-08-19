"""Shared HTTP resilience for enrichment: ride out transient upstream failures.

Extracted from enrich/profiles.py after the first production harvest, where
GitHub answered with secondary-rate-limit 403s, bare 502s, and 200s with
garbage bodies. Every enrichment fetcher (GraphQL profiles, commit-event
mining, LinkedIn resolution) goes through the same policy so a fix lands once.
"""

from __future__ import annotations

import asyncio
from typing import Callable

import httpx

TRANSIENT_STATUSES = {403, 429, 500, 502, 503, 504}
MAX_TRANSIENT_RETRIES = 6


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    log,
    on_request: Callable[[], None] | None = None,
    **kwargs,
) -> httpx.Response:
    """Issue a request, retrying transient statuses with backoff.

    Retry-After wins when present (capped at 120s). Raises the last response's
    status when retries are exhausted; non-transient statuses raise immediately.
    """
    delay = 2.0
    for attempt in range(MAX_TRANSIENT_RETRIES):
        if on_request is not None:
            on_request()
        resp = await client.request(method, url, **kwargs)
        if resp.status_code not in TRANSIENT_STATUSES:
            resp.raise_for_status()
            return resp
        retry_after = resp.headers.get("retry-after")
        sleep = float(retry_after) if retry_after else delay
        log.info(
            "http.transient",
            status=resp.status_code,
            attempt=attempt + 1,
            sleep=round(sleep, 1),
            url=str(url)[:80],
        )
        await asyncio.sleep(min(sleep, 120.0))
        delay = min(delay * 2, 90.0)
    resp.raise_for_status()  # out of retries: surface the last status
    return resp
