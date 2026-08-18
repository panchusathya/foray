"""Thin wrapper over the Anthropic Messages API.

Design rules:
  * every call is optional — if no API key is configured, ``available`` is False
    and callers fall back to the deterministic heuristics. The product works
    end-to-end offline; the model makes it better, not possible.
  * structured calls always go through ``output_config.format`` so we never
    parse JSON out of prose.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from ..config import get_settings

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]


class LLMUnavailable(RuntimeError):
    """Raised when a model call is attempted with no configured backend."""


@dataclass
class LLMResult:
    text: str
    data: dict[str, Any] | None
    model: str
    stop_reason: str | None
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient:
    """Sync + async access to Claude, with graceful degradation."""

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._api_key = api_key if api_key is not None else settings.anthropic_api_key
        self.model = model or settings.llm_model
        self.fast_model = settings.llm_fast_model
        self._enabled = settings.llm_enabled
        self._timeout = settings.llm_timeout_seconds
        self._semaphore = asyncio.Semaphore(max(1, settings.llm_max_concurrency))
        self._sync_client = None
        self._async_client = None

    # -- availability ----------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self._enabled and self._api_key and anthropic is not None)

    def _sync(self):
        if not self.available:
            raise LLMUnavailable("Anthropic client is not configured")
        if self._sync_client is None:
            self._sync_client = anthropic.Anthropic(api_key=self._api_key, timeout=self._timeout)
        return self._sync_client

    def _async(self):
        if not self.available:
            raise LLMUnavailable("Anthropic client is not configured")
        if self._async_client is None:
            self._async_client = anthropic.AsyncAnthropic(
                api_key=self._api_key, timeout=self._timeout
            )
        return self._async_client

    # -- request assembly ------------------------------------------------

    @staticmethod
    def _build_kwargs(
        *,
        model: str,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None,
        effort: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        output_config: dict[str, Any] = {"effort": effort}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "output_config": output_config,
            "messages": [{"role": "user", "content": prompt}],
        }

    @staticmethod
    def _extract(response) -> LLMResult:
        stop_reason = getattr(response, "stop_reason", None)
        text_parts = [
            block.text for block in getattr(response, "content", []) if getattr(block, "type", "") == "text"
        ]
        text = "".join(text_parts).strip()
        usage = getattr(response, "usage", None)
        return LLMResult(
            text=text,
            data=None,
            model=getattr(response, "model", ""),
            stop_reason=stop_reason,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )

    @staticmethod
    def _decode(result: LLMResult) -> LLMResult:
        if result.stop_reason == "refusal":
            raise LLMUnavailable("model declined the request")
        if not result.text:
            raise LLMUnavailable("empty model response")
        try:
            result.data = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"model returned non-JSON output: {exc}") from exc
        return result

    # -- public API ------------------------------------------------------

    def json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        effort: str = "low",
        max_tokens: int = 8000,
        model: str | None = None,
    ) -> LLMResult:
        kwargs = self._build_kwargs(
            model=model or self.model,
            system=system,
            prompt=prompt,
            schema=schema,
            effort=effort,
            max_tokens=max_tokens,
        )
        response = self._sync().messages.create(**kwargs)
        return self._decode(self._extract(response))

    async def ajson(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        effort: str = "low",
        max_tokens: int = 8000,
        model: str | None = None,
    ) -> LLMResult:
        kwargs = self._build_kwargs(
            model=model or self.model,
            system=system,
            prompt=prompt,
            schema=schema,
            effort=effort,
            max_tokens=max_tokens,
        )
        async with self._semaphore:
            response = await self._async().messages.create(**kwargs)
        return self._decode(self._extract(response))

    def text(
        self,
        *,
        system: str,
        prompt: str,
        effort: str = "low",
        max_tokens: int = 4000,
        model: str | None = None,
    ) -> LLMResult:
        kwargs = self._build_kwargs(
            model=model or self.model,
            system=system,
            prompt=prompt,
            schema=None,
            effort=effort,
            max_tokens=max_tokens,
        )
        response = self._sync().messages.create(**kwargs)
        result = self._extract(response)
        if result.stop_reason == "refusal":
            raise LLMUnavailable("model declined the request")
        return result

    async def gather(self, coros: list, *, default: Any = None) -> list[Any]:
        """Run model calls concurrently; a failure yields ``default`` for that slot."""
        results = await asyncio.gather(*coros, return_exceptions=True)
        cleaned: list[Any] = []
        for item in results:
            if isinstance(item, BaseException):
                logger.warning("llm call failed: %s", item)
                cleaned.append(default)
            else:
                cleaned.append(item)
        return cleaned


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_llm() -> None:
    global _client
    _client = None
