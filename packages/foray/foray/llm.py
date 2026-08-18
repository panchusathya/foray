"""OpenRouter chat client (OpenAI-compatible REST, no SDK).

One tiny surface used by scoring and query parsing: system + user in, text out.
httpx only; retries on 429/5xx with exponential backoff via tenacity. There is
no batch API on OpenRouter, so bulk scoring fans out bounded-concurrency
requests instead (see score/run.py).
"""

from __future__ import annotations

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import settings

_HEADERS_EXTRA = {
    # OpenRouter attribution headers (optional, used for their dashboards).
    "HTTP-Referer": "https://app.goforay.io",
    "X-Title": "Foray sourcing engine",
}


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def _payload(system: str, user: str, *, model: str, max_tokens: int) -> dict:
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }


def _extract_text(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise ValueError(f"OpenRouter returned no choices: {str(data)[:300]}")
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def _auth_headers() -> dict:
    settings.require("openrouter_api_key")
    return {"Authorization": f"Bearer {settings.openrouter_api_key}", **_HEADERS_EXTRA}


@retry(
    retry=retry_if_exception(_retryable),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, max=30),
    reraise=True,
)
def chat(system: str, user: str, *, model: str, max_tokens: int = 400, timeout: float = 90.0) -> str:
    """Synchronous single completion. Raises on non-retryable failure."""
    with httpx.Client(base_url=settings.openrouter_base_url, timeout=timeout) as client:
        resp = client.post(
            "/chat/completions",
            headers=_auth_headers(),
            json=_payload(system, user, model=model, max_tokens=max_tokens),
        )
        resp.raise_for_status()
        return _extract_text(resp.json())


@retry(
    retry=retry_if_exception(_retryable),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, max=30),
    reraise=True,
)
async def achat(
    client: httpx.AsyncClient, system: str, user: str, *, model: str, max_tokens: int = 400
) -> str:
    """Async single completion on a shared client (bulk scoring fan-out)."""
    resp = await client.post(
        "/chat/completions",
        headers=_auth_headers(),
        json=_payload(system, user, model=model, max_tokens=max_tokens),
    )
    resp.raise_for_status()
    return _extract_text(resp.json())
