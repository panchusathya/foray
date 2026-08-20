"""PeopleGPT-style search endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..deps import CommittingRoute, Principal, get_db, get_principal
from ..models import Candidate, Search
from ..schemas import (
    CandidateSummary,
    CriterionOut,
    ParsedQueryOut,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from ..search.engine import ScoredCandidate, get_engine
from ..search.index import CandidateDoc, get_index

router = APIRouter(route_class=CommittingRoute, prefix="/v1", tags=["search"])


def _summary_from_doc(doc: CandidateDoc) -> CandidateSummary:
    return CandidateSummary(
        id=doc.id,
        full_name=doc.full_name,
        headline=doc.headline,
        current_title=doc.current_title,
        current_company=doc.current_company,
        location_city=doc.location_city,
        location_country=doc.location_country,
        seniority=doc.seniority,
        years_experience=doc.years_experience,
        skills=sorted(doc.skills),
        linkedin_url=doc.linkedin_url,
        emails=doc.emails,
    )


def _hit(candidate: ScoredCandidate) -> SearchHit:
    return SearchHit(
        rank=candidate.rank,
        candidate=_summary_from_doc(candidate.doc),
        final_score=round(candidate.final_score, 4),
        heuristic_score=round(candidate.heuristic_score, 4),
        fusion_score=round(candidate.fusion_score, 4),
        lexical_score=round(candidate.lexical_score, 4),
        vector_score=round(candidate.vector_score, 4),
        rerank_score=(
            round(candidate.rerank_score, 4) if candidate.rerank_score is not None else None
        ),
        summary=candidate.summary,
        concerns=candidate.concerns,
        criteria=[CriterionOut(**c.to_dict()) for c in candidate.criteria],
    )


# Defined as a sync route on purpose: the engine does blocking DB and model work,
# so FastAPI runs it in the threadpool instead of stalling the event loop.
@router.post("/search", response_model=SearchResponse)
def search(
    payload: SearchRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> SearchResponse:
    outcome = get_engine().search(
        db,
        org_id=principal.org_id,
        query=payload.query,
        limit=payload.limit,
        overrides=(
            payload.overrides.model_dump(exclude_none=True) if payload.overrides else None
        ),
        use_rerank=payload.rerank,
        project_id=payload.project_id,
        persist=payload.persist,
        created_by=principal.user_id,
    )
    return SearchResponse(
        search_id=outcome.search_id,
        query=outcome.query,
        parser=outcome.parser,
        reranked=outcome.reranked,
        parsed=ParsedQueryOut(**outcome.parsed.to_dict()),
        total_matched=outcome.total_matched,
        latency_ms=outcome.latency_ms,
        results=[_hit(c) for c in outcome.results],
    )


@router.get("/searches", response_model=list[dict])
def list_searches(
    limit: int = 20,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = db.scalars(
        select(Search)
        .where(Search.org_id == principal.org_id)
        .order_by(Search.created_at.desc())
        .limit(min(limit, 100))
    ).all()
    return [
        {
            "id": row.id,
            "query": row.raw_query,
            "parser": row.parser,
            "reranked": row.reranked,
            "result_count": row.result_count,
            "latency_ms": row.latency_ms,
            "project_id": row.project_id,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/searches/{search_id}", response_model=SearchResponse)
def get_search(
    search_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> SearchResponse:
    search_row = db.scalar(
        select(Search)
        .where(Search.id == search_id, Search.org_id == principal.org_id)
        .options(selectinload(Search.results))
    )
    if search_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found")

    candidates = {
        c.id: c
        for c in db.scalars(
            select(Candidate)
            .where(Candidate.id.in_([r.candidate_id for r in search_row.results]))
            .options(selectinload(Candidate.experiences), selectinload(Candidate.educations))
        )
    }

    from ..search.index import candidate_to_doc

    hits: list[SearchHit] = []
    for row in sorted(search_row.results, key=lambda r: r.rank):
        candidate = candidates.get(row.candidate_id)
        if candidate is None:
            continue
        evidence = row.evidence or {}
        hits.append(
            SearchHit(
                rank=row.rank,
                candidate=_summary_from_doc(candidate_to_doc(candidate)),
                final_score=row.final_score,
                heuristic_score=row.heuristic_score,
                fusion_score=row.fusion_score,
                lexical_score=row.lexical_score,
                vector_score=row.vector_score,
                rerank_score=row.rerank_score,
                summary=evidence.get("summary", ""),
                concerns=evidence.get("concerns", []),
                criteria=[CriterionOut(**c) for c in evidence.get("criteria", [])],
            )
        )

    return SearchResponse(
        search_id=search_row.id,
        query=search_row.raw_query,
        parser=search_row.parser,
        reranked=search_row.reranked,
        parsed=ParsedQueryOut(**(search_row.parsed_query or {})),
        total_matched=search_row.result_count,
        latency_ms=search_row.latency_ms,
        results=hits,
    )


@router.get("/search/index-stats")
def index_stats(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> dict:
    return get_index(db, principal.org_id).stats()
