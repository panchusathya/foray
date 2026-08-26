"""Cross-encoder inference with a load-shed, a wall-clock timeout, and a breaker.

``asyncio.wait_for`` cancels the future, not the worker thread — the CPU work
runs to completion after we already returned a degraded page. So we gate on a
semaphore with a short acquire timeout before submitting, then a wall-clock
timeout on the result. One executor worker with 2 torch threads is the right
shape on 2 vCPU.

Any failure returns no logits (the caller keeps the un-reranked blend) and a
reason the envelope can surface: disabled | timeout | queue_full | failed.
``below_head`` is decided by the caller via ``needs_rerank`` before we run.

The breaker is time-boxed. It has to be: opening it makes ``rerank`` return
before any inference is attempted, so nothing can ever record the success that
closes it again. A transient blip -- three requests hitting the 1200 ms timeout
during a load spike -- otherwise disabled reranking for the life of the
process. After the cooldown one call is allowed through to probe.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..config import settings
from ..log import get_logger
from .cache import TtlLru, query_key

log = get_logger("rerank.runner")

Scorer = Callable[[list[tuple[str, str]]], list[float]]

_executor: ThreadPoolExecutor | None = None
_semaphore: asyncio.Semaphore | None = None
_cache: TtlLru | None = None
_consecutive_failures = 0
_breaker_open = False
_breaker_opened_at = 0.0


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="foray-ce")
    return _executor


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(1)
    return _semaphore


def _get_cache() -> TtlLru:
    global _cache
    if _cache is None:
        _cache = TtlLru(settings.rerank_cache_entries, float(settings.rerank_cache_ttl_s))
    return _cache


def reset_for_tests() -> None:
    global _consecutive_failures, _breaker_open, _breaker_opened_at, _cache, _semaphore
    _consecutive_failures = 0
    _breaker_open = False
    _breaker_opened_at = 0.0
    _cache = TtlLru(settings.rerank_cache_entries, float(settings.rerank_cache_ttl_s))
    _semaphore = asyncio.Semaphore(1)


@dataclass
class RerankOutcome:
    logits: dict[int, float] = field(default_factory=dict)
    reason: str = "disabled"
    model: str = ""


def _record_success() -> None:
    global _consecutive_failures, _breaker_open
    _consecutive_failures = 0
    _breaker_open = False


def _record_failure() -> None:
    global _consecutive_failures, _breaker_open, _breaker_opened_at
    _consecutive_failures += 1
    if _consecutive_failures >= settings.rerank_failure_threshold:
        _breaker_open = True
        _breaker_opened_at = time.monotonic()
        log.warning("rerank.circuit_open", failures=_consecutive_failures)


def _breaker_blocks() -> bool:
    """Whether the breaker refuses this call, half-opening it if it is time.

    The failure counter is deliberately NOT reset here: a probe that fails goes
    straight back over the threshold in _record_failure, which re-opens the
    breaker and restarts the cooldown, while a probe that succeeds clears
    everything. That is the whole state machine -- one flag and one timestamp.

    Concurrent callers can all slip through the same half-open window. The
    semaphore already admits one inference at a time and the rest degrade with
    queue_full, so the blast radius of that race is one extra timeout, not a
    thundering herd.
    """
    global _breaker_open
    if not _breaker_open:
        return False
    if time.monotonic() - _breaker_opened_at < settings.rerank_breaker_cooldown_s:
        return True
    _breaker_open = False
    log.info("rerank.circuit_half_open", failures=_consecutive_failures)
    return False


def _default_scorer(pairs: list[tuple[str, str]]) -> list[float]:
    from .model import predict_logits

    return predict_logits(pairs)


async def rerank(
    query: str,
    documents: Mapping[int, str],
    *,
    scorer: Scorer | None = None,
    ids: Sequence[int] | None = None,
) -> RerankOutcome:
    model_name = settings.rerank_model
    if not settings.rerank_enabled:
        return RerankOutcome(reason="disabled", model=model_name)
    if _breaker_blocks():
        return RerankOutcome(reason="failed", model=model_name)

    ordered_ids = list(ids if ids is not None else documents.keys())
    cache = _get_cache()
    qkey = query_key(query, model_name, settings.rerank_max_length)
    logits: dict[int, float] = {}
    missing: list[int] = []
    for cid in ordered_ids:
        cached = cache.get((qkey, cid))
        if cached is None:
            missing.append(cid)
        else:
            logits[cid] = cached
    if not missing:
        return RerankOutcome(logits=logits, reason="ok", model=model_name)

    pairs = [(query, documents[cid]) for cid in missing if cid in documents]
    missing = [cid for cid in missing if cid in documents]
    if not pairs:
        return RerankOutcome(logits=logits, reason="ok", model=model_name)

    predict = scorer or _default_scorer
    semaphore = _get_semaphore()
    queue_timeout = settings.rerank_queue_timeout_ms / 1000.0
    infer_timeout = settings.rerank_timeout_ms / 1000.0

    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=queue_timeout)
    except TimeoutError:
        return RerankOutcome(logits=logits, reason="queue_full", model=model_name)

    # Hold the semaphore until the worker thread actually finishes. Releasing
    # on wait_for timeout would overlap inferences on 2 vCPU — the cancelled
    # future does not stop the thread.
    released = False

    def _release(_fut: object | None = None) -> None:
        nonlocal released
        if not released:
            released = True
            semaphore.release()

    loop = asyncio.get_running_loop()
    started = time.monotonic()
    worker = loop.run_in_executor(_get_executor(), predict, pairs)
    try:
        scores = await asyncio.wait_for(asyncio.shield(worker), timeout=infer_timeout)
    except TimeoutError:
        log.info("rerank.timeout", elapsed_ms=int((time.monotonic() - started) * 1000))
        worker.add_done_callback(_release)
        _record_failure()
        return RerankOutcome(logits=logits, reason="timeout", model=model_name)
    except Exception as exc:  # noqa: BLE001 - degrade, never fail the search
        log.info("rerank.failed", error=str(exc))
        _release()
        _record_failure()
        return RerankOutcome(logits=logits, reason="failed", model=model_name)

    _release()
    if len(scores) != len(missing):
        _record_failure()
        return RerankOutcome(logits=logits, reason="failed", model=model_name)

    _record_success()
    for cid, score in zip(missing, scores, strict=True):
        logits[cid] = float(score)
        cache.put((qkey, cid), float(score))
    return RerankOutcome(logits=logits, reason="ok", model=model_name)
