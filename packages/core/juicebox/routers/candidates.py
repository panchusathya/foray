"""Candidate ingestion and retrieval."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..deps import Principal, get_db, get_principal, require_write
from ..enrich.email import guess_emails
from ..models import Candidate
from ..repository import upsert_candidates
from ..schemas import (
    BulkCandidateIn,
    BulkUpsertResult,
    CandidateIn,
    CandidateOut,
    CandidateSummary,
)

router = APIRouter(prefix="/v1/candidates", tags=["candidates"])

MAX_BULK = 5000


@router.post("", response_model=CandidateOut, status_code=status.HTTP_201_CREATED)
def create_candidate(
    payload: CandidateIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> CandidateOut:
    result = upsert_candidates(
        db, org_id=principal.org_id, payloads=[payload.model_dump(exclude_none=True)]
    )
    if not result.ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Candidate could not be parsed"
        )
    candidate = db.get(Candidate, result.ids[0])
    return CandidateOut.model_validate(candidate)


@router.post("/bulk", response_model=BulkUpsertResult)
def bulk_upsert(
    payload: BulkCandidateIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> BulkUpsertResult:
    if len(payload.candidates) > MAX_BULK:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Send at most {MAX_BULK} candidates per request",
        )
    result = upsert_candidates(
        db,
        org_id=principal.org_id,
        payloads=[c.model_dump(exclude_none=True) for c in payload.candidates],
    )
    return BulkUpsertResult(created=result.created, updated=result.updated, ids=result.ids)


@router.get("", response_model=list[CandidateSummary])
def list_candidates(
    q: str | None = Query(default=None, description="Substring match on name, title or company"),
    skill: list[str] | None = Query(default=None),
    seniority: list[str] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[CandidateSummary]:
    stmt = select(Candidate).where(Candidate.org_id == principal.org_id)
    if q:
        pattern = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Candidate.full_name).like(pattern),
                func.lower(Candidate.current_title).like(pattern),
                func.lower(Candidate.current_company).like(pattern),
                func.lower(Candidate.headline).like(pattern),
            )
        )
    if seniority:
        stmt = stmt.where(Candidate.seniority.in_(seniority))

    rows = db.scalars(
        stmt.order_by(Candidate.full_name).limit(limit).offset(offset)
    ).all()

    if skill:
        # skills live in a JSON array; filter in Python to stay portable across
        # SQLite and Postgres rather than depending on a JSON operator.
        wanted = {s.lower() for s in skill}
        rows = [r for r in rows if wanted & {s.lower() for s in (r.skills or [])}]

    return [CandidateSummary.model_validate(r) for r in rows]


@router.get("/count")
def count_candidates(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> dict[str, int]:
    total = db.scalar(
        select(func.count(Candidate.id)).where(Candidate.org_id == principal.org_id)
    )
    return {"count": int(total or 0)}


@router.get("/{candidate_id}", response_model=CandidateOut)
def get_candidate(
    candidate_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> CandidateOut:
    candidate = db.scalar(
        select(Candidate)
        .where(Candidate.id == candidate_id, Candidate.org_id == principal.org_id)
        .options(selectinload(Candidate.experiences), selectinload(Candidate.educations))
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return CandidateOut.model_validate(candidate)


@router.get("/{candidate_id}/email-guesses")
def email_guesses(
    candidate_id: str,
    domain: str | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict:
    candidate = db.scalar(
        select(Candidate).where(
            Candidate.id == candidate_id, Candidate.org_id == principal.org_id
        )
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")

    target_domain = domain or candidate.current_company_domain
    guesses = guess_emails(candidate.first_name, candidate.last_name, target_domain or "")
    return {
        "known": candidate.emails,
        "domain": target_domain,
        "guesses": [
            {"email": g.email, "pattern": g.pattern, "confidence": g.confidence}
            for g in guesses
        ],
    }


@router.delete("/{candidate_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_candidate(
    candidate_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> None:
    from ..search.index import invalidate

    candidate = db.scalar(
        select(Candidate).where(
            Candidate.id == candidate_id, Candidate.org_id == principal.org_id
        )
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    db.delete(candidate)
    invalidate(principal.org_id)
