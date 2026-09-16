"""The search pipeline.

    natural language
        -> parse (LLM w/ heuristic fallback)
        -> hard filters
        -> hybrid retrieval (BM25 + embeddings, fused with RRF)
        -> deterministic fit scoring w/ evidence
        -> LLM rerank of the shortlist
        -> blended final score

Every stage is optional except filters and scoring, so the engine degrades
cleanly from "full stack with a model" down to "offline keyword search".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Search, SearchResult, new_id
from .index import CandidateDoc, get_index
from .parser import ParsedQuery, parse_query
from .rerank import RerankResult, rerank
from .scoring import Criterion, FitAssessment, assess, requirement_list

logger = logging.getLogger(__name__)


@dataclass
class ScoredCandidate:
    doc: CandidateDoc
    rank: int = 0
    lexical_score: float = 0.0
    vector_score: float = 0.0
    fusion_score: float = 0.0
    heuristic_score: float = 0.0
    rerank_score: float | None = None
    final_score: float = 0.0
    summary: str = ""
    concerns: list[str] = field(default_factory=list)
    criteria: list[Criterion] = field(default_factory=list)

    def evidence_payload(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "concerns": self.concerns,
            "criteria": [c.to_dict() for c in self.criteria],
        }


@dataclass
class SearchOutcome:
    query: str
    parsed: ParsedQuery
    parser: str
    reranked: bool
    results: list[ScoredCandidate]
    total_matched: int
    latency_ms: int
    excluded: dict[str, int] = field(default_factory=dict)
    search_id: str | None = None


def _merge_criteria(
    base: list[Criterion], from_model: list[Criterion]
) -> list[Criterion]:
    """Model criteria replace the qualitative placeholders and append the rest."""
    kept = [c for c in base if c.kind != "freeform"]
    labels = {c.label.strip().lower() for c in kept}
    for criterion in from_model:
        if criterion.label.strip().lower() in labels:
            continue
        kept.append(criterion)
    return kept


class SearchEngine:
    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()

    # -- pipeline --------------------------------------------------------

    def search(
        self,
        db: Session,
        *,
        org_id: str,
        query: str,
        limit: int = 25,
        overrides: dict[str, Any] | None = None,
        use_rerank: bool | None = None,
        project_id: str | None = None,
        persist: bool = True,
        created_by: str | None = None,
        only_available: bool = False,
    ) -> SearchOutcome:
        started = time.perf_counter()
        settings = self.settings

        parsed, parser_name = parse_query(query, overrides=overrides)
        index = get_index(db, org_id)

        if len(index) == 0:
            return SearchOutcome(
                query=query,
                parsed=parsed,
                parser=parser_name,
                reranked=False,
                results=[],
                total_matched=0,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        pool = index.search_pool(
            parsed,
            pool_size=max(settings.retrieval_candidate_pool, limit),
            only_available=only_available,
        )
        if pool.total_matched == 0:
            return SearchOutcome(
                query=query,
                parsed=parsed,
                parser=parser_name,
                reranked=False,
                results=[],
                total_matched=0,
                latency_ms=int((time.perf_counter() - started) * 1000),
                excluded=pool.excluded,
            )

        scored: list[ScoredCandidate] = []
        for position, doc in enumerate(pool.docs):
            assessment: FitAssessment = assess(doc, parsed)
            scored.append(
                ScoredCandidate(
                    doc=doc,
                    lexical_score=float(pool.lexical[position]),
                    vector_score=float(pool.vector[position]),
                    fusion_score=float(pool.fusion[position]),
                    heuristic_score=assessment.score,
                    criteria=list(assessment.criteria),
                )
            )

        # Provisional ordering decides who is worth spending model tokens on.
        for candidate in scored:
            candidate.final_score = self._blend(candidate)
        scored.sort(key=lambda c: c.final_score, reverse=True)

        did_rerank = False
        want_rerank = settings.rerank_top_k > 0 if use_rerank is None else use_rerank
        if want_rerank:
            shortlist = scored[: min(settings.rerank_top_k, max(limit, 1))]
            verdicts = rerank([c.doc for c in shortlist], requirement_list(parsed))
            if verdicts:
                did_rerank = True
                self._apply_rerank(shortlist, verdicts)
                for candidate in scored:
                    candidate.final_score = self._blend(candidate)
                scored.sort(key=lambda c: c.final_score, reverse=True)

        results = scored[:limit]
        for rank, candidate in enumerate(results, start=1):
            candidate.rank = rank

        latency_ms = int((time.perf_counter() - started) * 1000)
        search_outcome = SearchOutcome(
            query=query,
            parsed=parsed,
            parser=parser_name,
            reranked=did_rerank,
            results=results,
            total_matched=pool.total_matched,
            latency_ms=latency_ms,
            excluded=pool.excluded,
        )

        if persist:
            search_outcome.search_id = self._persist(
                db,
                org_id=org_id,
                project_id=project_id,
                created_by=created_by,
                overrides=overrides or {},
                result=search_outcome,
            )
        return search_outcome

    def _blend(self, candidate: ScoredCandidate) -> float:
        settings = self.settings
        if candidate.rerank_score is None:
            weight_sum = settings.heuristic_weight + settings.retrieval_weight
            if weight_sum <= 0:
                return candidate.heuristic_score
            return (
                settings.heuristic_weight * candidate.heuristic_score
                + settings.retrieval_weight * candidate.fusion_score
            ) / weight_sum
        weight_sum = (
            settings.heuristic_weight + settings.retrieval_weight + settings.rerank_weight
        )
        if weight_sum <= 0:
            return candidate.heuristic_score
        return (
            settings.heuristic_weight * candidate.heuristic_score
            + settings.retrieval_weight * candidate.fusion_score
            + settings.rerank_weight * candidate.rerank_score
        ) / weight_sum

    @staticmethod
    def _apply_rerank(
        shortlist: list[ScoredCandidate], verdicts: dict[str, RerankResult]
    ) -> None:
        for candidate in shortlist:
            verdict = verdicts.get(candidate.doc.id)
            if verdict is None:
                continue
            candidate.rerank_score = verdict.score
            candidate.summary = verdict.summary
            candidate.concerns = verdict.concerns
            candidate.criteria = _merge_criteria(candidate.criteria, verdict.criteria)

    # -- persistence -----------------------------------------------------

    @staticmethod
    def _persist(
        db: Session,
        *,
        org_id: str,
        project_id: str | None,
        created_by: str | None,
        overrides: dict[str, Any],
        result: SearchOutcome,
    ) -> str:
        search = Search(
            id=new_id("srch"),
            org_id=org_id,
            project_id=project_id,
            raw_query=result.query,
            parsed_query=result.parsed.to_dict(),
            overrides=overrides,
            parser=result.parser,
            reranked=result.reranked,
            result_count=len(result.results),
            latency_ms=result.latency_ms,
            created_by=created_by,
        )
        db.add(search)
        for candidate in result.results:
            db.add(
                SearchResult(
                    search_id=search.id,
                    candidate_id=candidate.doc.id,
                    rank=candidate.rank,
                    lexical_score=candidate.lexical_score,
                    vector_score=candidate.vector_score,
                    fusion_score=candidate.fusion_score,
                    heuristic_score=candidate.heuristic_score,
                    rerank_score=candidate.rerank_score,
                    final_score=candidate.final_score,
                    evidence=candidate.evidence_payload(),
                )
            )
        db.flush()
        return search.id


_engine: SearchEngine | None = None


def get_engine() -> SearchEngine:
    global _engine
    if _engine is None:
        _engine = SearchEngine()
    return _engine


def reset_engine() -> None:
    global _engine
    _engine = None
