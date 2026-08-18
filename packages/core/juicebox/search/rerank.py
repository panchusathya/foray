"""LLM reranking of the top-K shortlist.

The reranker never changes *which* people are eligible — filters and retrieval
already decided that. It re-orders the shortlist and attaches human-readable
evidence, and it is skipped entirely when no model is configured.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..llm.client import get_llm
from ..llm.prompts import RERANK_SCHEMA, RERANK_SYSTEM, rerank_prompt
from .index import CandidateDoc
from .scoring import Criterion

logger = logging.getLogger(__name__)


@dataclass
class RerankResult:
    candidate_id: str
    score: float
    summary: str = ""
    concerns: list[str] = field(default_factory=list)
    criteria: list[Criterion] = field(default_factory=list)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _to_criteria(payload: dict) -> list[Criterion]:
    criteria: list[Criterion] = []
    for index, item in enumerate(payload.get("criteria") or []):
        requirement = str(item.get("requirement", "")).strip()
        if not requirement:
            continue
        status = str(item.get("status", "unknown"))
        if status not in {"strong", "partial", "miss", "unknown"}:
            status = "unknown"
        criteria.append(
            Criterion(
                name=f"llm:{index}",
                label=requirement,
                kind="llm",
                weight=1.0,
                status=status,
                score=_clamp(item.get("score", 0.0)),
                evidence=str(item.get("evidence", "")).strip(),
                sources=["model"],
            )
        )
    return criteria


async def _rerank_one(doc: CandidateDoc, requirements: list[str]) -> RerankResult | None:
    llm = get_llm()
    result = await llm.ajson(
        system=RERANK_SYSTEM,
        prompt=rerank_prompt(requirements=requirements, profile=doc.profile_text()),
        schema=RERANK_SCHEMA,
        effort="low",
        max_tokens=8000,
    )
    data = result.data or {}
    return RerankResult(
        candidate_id=doc.id,
        score=_clamp(data.get("overall_score", 0.0)),
        summary=str(data.get("summary", "")).strip(),
        concerns=[str(c).strip() for c in (data.get("concerns") or []) if str(c).strip()],
        criteria=_to_criteria(data),
    )


async def arerank(
    docs: list[CandidateDoc], requirements: list[str]
) -> dict[str, RerankResult]:
    llm = get_llm()
    if not llm.available or not docs:
        return {}
    results = await llm.gather([_rerank_one(doc, requirements) for doc in docs], default=None)
    return {r.candidate_id: r for r in results if isinstance(r, RerankResult)}


def rerank(docs: list[CandidateDoc], requirements: list[str]) -> dict[str, RerankResult]:
    """Sync entry point. Safe to call from a threadpool-backed FastAPI route."""
    if not docs:
        return {}
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(arerank(docs, requirements))
        except Exception as exc:  # noqa: BLE001 - reranking is best-effort
            logger.warning("rerank failed: %s", exc)
            return {}

    # Already inside an event loop (e.g. an async caller): run in a fresh one.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, arerank(docs, requirements))
        try:
            return future.result()
        except Exception as exc:  # noqa: BLE001
            logger.warning("rerank failed: %s", exc)
            return {}
