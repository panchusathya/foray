"""OpenRouter transport for the LLM gateway.

Slots in behind the same ``Transport`` protocol as the Anthropic SDK, so no
call site changes: the Anthropic-shaped kwargs that ``LLMClient._build_kwargs``
produces stay the internal representation, and the translation to OpenRouter's
chat-completions shape happens here.

Two details are load-bearing and were learned the hard way in the Foray
scoring path (``packages/foray/foray/llm.py``):

  * ``reasoning.effort`` is always sent. Reasoning-family models otherwise
    spend the completion budget on a hidden scratchpad and truncate the JSON
    mid-object, which surfaces as "model returned non-JSON output" rather than
    as a length error.
  * ``provider.require_parameters`` is always sent. Without it OpenRouter may
    route a structured request to a provider that silently ignores
    ``response_format`` and answers in prose.

httpx errors are re-raised as ``LLMTransportError`` carrying ``status_code``,
because ``client.is_retryable`` reads that attribute and ``httpx`` keeps the
status on ``.response`` — a 429 would otherwise be classified as fatal.
"""

from __future__ import annotations

from typing import Any

import httpx

from .client import LLMResult

# OpenRouter finish reasons -> the Anthropic vocabulary the rest of the code
# already branches on (notably ``refusal``, which _decode turns into a clean
# LLMUnavailable instead of a parse error).
_FINISH_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
    "tool_calls": "tool_use",
    "error": None,
}

_EFFORTS = {"low", "medium", "high"}

# Published per-million-token prices, used only when OpenRouter does not report
# a cost for the call. An unknown slug yields 0.0 rather than a wrong number.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "z-ai/glm-5.2": (0.50, 3.15),
    "z-ai/glm-5.3-flash": (0.075, 0.25),
    "z-ai/glm-4.6": (0.43, 1.75),
    "openai/gpt-oss-120b": (0.05, 0.25),
}


class LLMTransportError(RuntimeError):
    """HTTP failure from a non-SDK transport, carrying a retry-classifiable status."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def estimated_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = PRICE_PER_MTOK.get(model)
    if prices is None:
        return 0.0
    prompt_price, completion_price = prices
    return (input_tokens * prompt_price + output_tokens * completion_price) / 1_000_000


def to_openrouter(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Translate internal (Anthropic-shaped) kwargs into an OpenRouter body."""
    output_config = kwargs.get("output_config") or {}
    effort = str(output_config.get("effort") or "low").lower()
    if effort not in _EFFORTS:
        effort = "low"

    messages: list[dict[str, Any]] = []
    system = kwargs.get("system")
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(kwargs.get("messages") or [])

    body: dict[str, Any] = {
        "model": kwargs["model"],
        "max_tokens": kwargs.get("max_tokens", 4000),
        "messages": messages,
        "reasoning": {"effort": effort},
        "usage": {"include": True},
    }

    schema = (output_config.get("format") or {}).get("schema")
    if schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "result",
                # Only claim strict when the schema actually closes the root
                # object; claiming it otherwise makes providers reject the call.
                "strict": schema.get("additionalProperties") is False,
                "schema": schema,
            },
        }
        body["provider"] = {"require_parameters": True}
    return body


def from_openrouter(payload: dict[str, Any], *, fallback_model: str = "") -> LLMResult:
    choices = payload.get("choices") or []
    if not choices:
        error = payload.get("error") or {}
        detail = error.get("message") or "no choices in response"
        raise LLMTransportError(f"openrouter returned no completion: {detail}")

    choice = choices[0] or {}
    message = choice.get("message") or {}
    text = (message.get("content") or "").strip()
    finish = choice.get("finish_reason")
    usage = payload.get("usage") or {}

    return LLMResult(
        text=text,
        data=None,
        model=str(payload.get("model") or fallback_model),
        stop_reason=_FINISH_REASONS.get(str(finish), finish),
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )


def reported_cost_usd(payload: dict[str, Any]) -> float | None:
    """OpenRouter's own cost for the call, when it reports one."""
    usage = payload.get("usage") or {}
    cost = usage.get("cost")
    try:
        return float(cost) if cost is not None else None
    except (TypeError, ValueError):
        return None


class OpenRouterTransport:
    name = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float,
        referer: str = "https://app.goforay.io",
        title: str = "Foray",
        client: httpx.Client | None = None,
        async_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Attribution, per OpenRouter's guidance.
            "HTTP-Referer": referer,
            "X-Title": title,
        }
        self._client = client
        self._async_client = async_client
        # Cost of the most recent call, read by LLMClient when tallying spend.
        self.last_cost_usd: float | None = None

    # -- clients ---------------------------------------------------------

    def _sync(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _async(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=self._timeout)
        return self._async_client

    # -- requests --------------------------------------------------------

    def _url(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _finish(self, response: httpx.Response, model: str) -> LLMResult:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LLMTransportError(
                f"openrouter HTTP {exc.response.status_code}: {exc.response.text[:300]}",
                status_code=exc.response.status_code,
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMTransportError(f"openrouter returned non-JSON body: {exc}") from exc
        self.last_cost_usd = reported_cost_usd(payload)
        return from_openrouter(payload, fallback_model=model)

    def complete(self, kwargs: dict[str, Any]) -> LLMResult:
        body = to_openrouter(kwargs)
        timeout = kwargs.get("timeout")
        try:
            response = self._sync().post(
                self._url(),
                json=body,
                headers=self._headers,
                **({"timeout": timeout} if timeout is not None else {}),
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"openrouter request timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"openrouter transport error: {exc}") from exc
        return self._finish(response, body["model"])

    async def acomplete(self, kwargs: dict[str, Any]) -> LLMResult:
        body = to_openrouter(kwargs)
        timeout = kwargs.get("timeout")
        try:
            response = await self._async().post(
                self._url(),
                json=body,
                headers=self._headers,
                **({"timeout": timeout} if timeout is not None else {}),
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"openrouter request timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"openrouter transport error: {exc}") from exc
        return self._finish(response, body["model"])
