"""Thin gateway over the Anthropic Messages API.

Design rules:
  * every call is optional — if no API key is configured, ``available`` is False
    and callers fall back to the deterministic heuristics. The product works
    end-to-end offline; the model makes it better, not possible.
  * structured calls always go through ``output_config.format`` so we never
    parse JSON out of prose.
  * the SDK call sits behind ``Transport`` so tests can drive every branch
    without a network or an API key.

``output_config`` carries ``effort`` on every request, which the Anthropic SDK
only models from 0.78 onwards. An older SDK installs happily and then fails
every call, so the capability is checked up front and reported as unavailable
rather than discovered one silent fallback at a time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from ..config import get_settings

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]

MIN_ANTHROPIC_VERSION = "0.78"


class LLMUnavailable(RuntimeError):
    """Raised when a model call is attempted with no usable backend."""


@dataclass
class LLMResult:
    text: str
    data: dict[str, Any] | None
    model: str
    stop_reason: str | None
    input_tokens: int = 0
    output_tokens: int = 0


# --------------------------------------------------------------------------
# sdk capability
# --------------------------------------------------------------------------


def sdk_capability() -> tuple[bool, str]:
    """Can the installed SDK express the requests we build? (ok, reason)."""
    if anthropic is None:
        return False, "the anthropic package is not installed"
    try:
        from anthropic.types.output_config_param import OutputConfigParam
    except Exception:  # noqa: BLE001 - any import shape change means too old
        return (
            False,
            f"anthropic {getattr(anthropic, '__version__', '?')} has no output_config; "
            f"juicebox needs >={MIN_ANTHROPIC_VERSION}",
        )
    fields = set(getattr(OutputConfigParam, "__annotations__", {}))
    missing = {"effort", "format"} - fields
    if missing:
        return (
            False,
            f"anthropic {getattr(anthropic, '__version__', '?')} output_config is missing "
            f"{sorted(missing)}; juicebox needs >={MIN_ANTHROPIC_VERSION}",
        )
    return True, ""


# --------------------------------------------------------------------------
# cost ledger
# --------------------------------------------------------------------------


@dataclass
class ModelUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class CostLedger:
    """In-process tally of model spend. Reset per process, not persisted."""

    calls: int = 0
    failures: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_error: str = ""
    by_model: dict[str, ModelUsage] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def record_success(self, model: str, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            usage = self.by_model.setdefault(model or "unknown", ModelUsage())
            usage.calls += 1
            usage.input_tokens += input_tokens
            usage.output_tokens += output_tokens

    def record_retry(self) -> None:
        with self._lock:
            self.retries += 1

    def record_failure(self, error: str) -> None:
        with self._lock:
            self.failures += 1
            self.last_error = error

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": self.calls,
                "failures": self.failures,
                "retries": self.retries,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "last_error": self.last_error,
                "by_model": {name: asdict(usage) for name, usage in self.by_model.items()},
            }


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------


class Transport(Protocol):
    name: str

    def complete(self, kwargs: dict[str, Any]) -> LLMResult:  # pragma: no cover - protocol
        ...

    async def acomplete(self, kwargs: dict[str, Any]) -> LLMResult:  # pragma: no cover
        ...


def _extract(response: Any) -> LLMResult:
    stop_reason = getattr(response, "stop_reason", None)
    text_parts = [
        block.text
        for block in getattr(response, "content", [])
        if getattr(block, "type", "") == "text"
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


class AnthropicTransport:
    """The real SDK call. One client pair, created lazily."""

    name = "anthropic"

    def __init__(self, *, api_key: str, timeout: float) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._sync_client: Any = None
        self._async_client: Any = None

    def _sync(self) -> Any:
        if self._sync_client is None:
            self._sync_client = anthropic.Anthropic(api_key=self._api_key, timeout=self._timeout)
        return self._sync_client

    def _async(self) -> Any:
        if self._async_client is None:
            self._async_client = anthropic.AsyncAnthropic(
                api_key=self._api_key, timeout=self._timeout
            )
        return self._async_client

    def complete(self, kwargs: dict[str, Any]) -> LLMResult:
        return _extract(self._sync().messages.create(**kwargs))

    async def acomplete(self, kwargs: dict[str, Any]) -> LLMResult:
        return _extract(await self._async().messages.create(**kwargs))


class FakeTransport:
    """Scripted transport for tests.

    ``responses`` is consumed in order. Each entry may be a dict (serialised as
    the JSON body), a string (raw text), an ``LLMResult``, an exception to
    raise, or a callable taking the request kwargs. ``default`` is used once the
    script runs out.
    """

    name = "fake"

    def __init__(self, responses: list[Any] | None = None, *, default: Any = None) -> None:
        self._queue: list[Any] = list(responses or [])
        self._default = default
        self.calls: list[dict[str, Any]] = []

    def _result(self, item: Any, kwargs: dict[str, Any]) -> LLMResult:
        if isinstance(item, LLMResult):
            return item
        text = json.dumps(item) if isinstance(item, dict | list) else str(item)
        prompt = "".join(
            str(message.get("content", "")) for message in kwargs.get("messages", [])
        )
        return LLMResult(
            text=text,
            data=None,
            model=str(kwargs.get("model", "fake-model")),
            stop_reason="end_turn",
            input_tokens=len(prompt.split()),
            output_tokens=len(text.split()),
        )

    def _next(self, kwargs: dict[str, Any]) -> LLMResult:
        self.calls.append(kwargs)
        if self._queue:
            item = self._queue.pop(0)
        elif self._default is not None:
            item = self._default
        else:
            raise LLMUnavailable("FakeTransport has no scripted response left")
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(kwargs)
        return self._result(item, kwargs)

    def complete(self, kwargs: dict[str, Any]) -> LLMResult:
        return self._next(kwargs)

    async def acomplete(self, kwargs: dict[str, Any]) -> LLMResult:
        return self._next(kwargs)


# --------------------------------------------------------------------------
# retry classification
# --------------------------------------------------------------------------


def is_retryable(exc: BaseException) -> bool:
    """Rate limits, transient server errors and connection problems only."""
    if isinstance(exc, LLMUnavailable):
        return False
    if anthropic is not None:
        for attribute in ("APITimeoutError", "APIConnectionError", "RateLimitError"):
            error_type = getattr(anthropic, attribute, None)
            if error_type is not None and isinstance(exc, error_type):
                return True
        status_error = getattr(anthropic, "APIStatusError", None)
        if status_error is not None and isinstance(exc, status_error):
            status = getattr(exc, "status_code", None)
            return status == 429 or (isinstance(status, int) and status >= 500)
    status = getattr(exc, "status_code", None)
    if status == 429 or (isinstance(status, int) and status >= 500):
        return True
    return isinstance(exc, TimeoutError | ConnectionError)


class LLMClient:
    """Sync + async access to Claude, with graceful degradation."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        transport: Transport | None = None,
        enabled: bool | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key if api_key is not None else settings.anthropic_api_key
        self.model = model or settings.llm_model
        self.fast_model = settings.llm_fast_model
        self._enabled = settings.llm_enabled if enabled is None else enabled
        self._timeout = settings.llm_timeout_seconds
        self._semaphore = asyncio.Semaphore(max(1, settings.llm_max_concurrency))
        self._max_retries = max(0, settings.llm_max_retries)
        self._initial_delay = max(0.0, settings.llm_retry_initial_delay)
        self._max_delay = max(0.0, settings.llm_retry_max_delay)
        self.ledger = CostLedger()

        self._transport = transport
        self._sdk_ok, self._sdk_reason = sdk_capability()
        if transport is None and self._enabled and self._api_key and not self._sdk_ok:
            # A key is configured, so the operator expects AI to be on. Saying so
            # once at error level beats N warnings from N silent fallbacks.
            logger.error("anthropic client disabled: %s", self._sdk_reason)

    # -- availability ----------------------------------------------------

    @property
    def available(self) -> bool:
        if not self._enabled:
            return False
        if self._transport is not None:
            return True
        return bool(self._api_key) and self._sdk_ok

    @property
    def unavailable_reason(self) -> str:
        if self.available:
            return ""
        if not self._enabled:
            return "JUICEBOX_LLM_ENABLED is false"
        if not self._api_key:
            return "ANTHROPIC_API_KEY is not set"
        return self._sdk_reason or "no usable backend"

    def _require_transport(self) -> Transport:
        if not self.available:
            raise LLMUnavailable(f"Anthropic client is not configured: {self.unavailable_reason}")
        if self._transport is None:
            self._transport = AnthropicTransport(api_key=self._api_key, timeout=self._timeout)
        return self._transport

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

    # -- retry loop ------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        if self._initial_delay <= 0:
            return 0.0
        delay = min(self._initial_delay * (2**attempt), self._max_delay)
        # Jitter so concurrent rerank calls don't retry in lockstep.
        return delay * (0.5 + random.random() / 2)

    def _finish(self, result: LLMResult) -> LLMResult:
        self.ledger.record_success(result.model, result.input_tokens, result.output_tokens)
        return result

    def _fail(self, exc: BaseException) -> LLMUnavailable:
        self.ledger.record_failure(f"{type(exc).__name__}: {exc}")
        if isinstance(exc, LLMUnavailable):
            return exc
        return LLMUnavailable(f"model call failed: {exc}")

    def _call(self, kwargs: dict[str, Any]) -> LLMResult:
        transport = self._require_transport()
        for attempt in range(self._max_retries + 1):
            try:
                return self._finish(transport.complete(kwargs))
            except Exception as exc:  # classified by is_retryable below
                if attempt >= self._max_retries or not is_retryable(exc):
                    raise self._fail(exc) from exc
                self.ledger.record_retry()
                delay = self._backoff(attempt)
                logger.warning(
                    "retrying model call after %s (attempt %d/%d, sleeping %.2fs)",
                    exc,
                    attempt + 1,
                    self._max_retries,
                    delay,
                )
                if delay:
                    time.sleep(delay)
        raise LLMUnavailable("model call failed")  # pragma: no cover - loop always returns

    async def _acall(self, kwargs: dict[str, Any]) -> LLMResult:
        transport = self._require_transport()
        for attempt in range(self._max_retries + 1):
            try:
                async with self._semaphore:
                    result = await transport.acomplete(kwargs)
                return self._finish(result)
            except Exception as exc:  # classified by is_retryable below
                if attempt >= self._max_retries or not is_retryable(exc):
                    raise self._fail(exc) from exc
                self.ledger.record_retry()
                delay = self._backoff(attempt)
                logger.warning(
                    "retrying model call after %s (attempt %d/%d, sleeping %.2fs)",
                    exc,
                    attempt + 1,
                    self._max_retries,
                    delay,
                )
                if delay:
                    await asyncio.sleep(delay)
        raise LLMUnavailable("model call failed")  # pragma: no cover - loop always returns

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
        return self._decode(self._call(kwargs))

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
        return self._decode(await self._acall(kwargs))

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
        result = self._call(kwargs)
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

    def usage(self) -> dict[str, Any]:
        return self.ledger.snapshot()


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def set_llm(client: LLMClient | None) -> None:
    """Tests inject a client built on ``FakeTransport``."""
    global _client
    _client = client


def reset_llm() -> None:
    global _client
    _client = None
