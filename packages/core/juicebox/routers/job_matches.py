"""Per-contact job matches and the rematch queue.

GET /rematch-queue is declared before /{match_id} so FastAPI does not treat
the literal as an id.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..jobmatch.queue import rematch_queue
from ..models import Candidate, JobMatch, JobPosting
from ..schemas import CandidateSummary, JobMatchOut, JobMatchPatch, JobPostingOut, RematchQueueRow

router = APIRouter(route_class=CommittingRoute, prefix="/v1/job-matches", tags=["job-matches"])


@router.get("/rematch-queue", response_model=list[RematchQueueRow])
def get_rematch_queue(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[RematchQueueRow]:
    rows = rematch_queue(db, principal.org_id, limit=limit)
    out: list[RematchQueueRow] = []
    for row in rows:
        matches = [_match_out(db, match) for match in row["matches"]]
        out.append(
            RematchQueueRow(
                candidate=CandidateSummary.model_validate(row["candidate"]),
                reply_intent=row["reply_intent"],
                declined_project_id=row["declined_project_id"],
                declined_label=row["declined_label"],
                declined_on=row["declined_on"],
                matches=matches,
            )
        )
    return out


@router.get("", response_model=list[JobMatchOut])
def list_matches(
    candidate_id: str = Query(...),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    min_score: float | None = Query(default=None),
) -> list[JobMatchOut]:
    candidate = db.scalar(
        select(Candidate).where(
            Candidate.id == candidate_id, Candidate.org_id == principal.org_id
        )
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    floor = min_score if min_score is not None else get_settings().job_match_min_score
    rows = db.scalars(
        select(JobMatch)
        .where(
            JobMatch.candidate_id == candidate_id,
            JobMatch.org_id == principal.org_id,
            JobMatch.status != "dismissed",
            JobMatch.score >= floor,
        )
        .order_by(JobMatch.score.desc())
    ).all()
    return [_match_out(db, row, candidate=candidate) for row in rows]


@router.patch("/{match_id}", response_model=JobMatchOut)
def patch_match(
    match_id: str,
    payload: JobMatchPatch,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> JobMatchOut:
    row = db.scalar(
        select(JobMatch).where(JobMatch.id == match_id, JobMatch.org_id == principal.org_id)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found")
    row.status = payload.status
    if payload.status == "dismissed":
        row.dismissed_reason = payload.dismissed_reason or row.dismissed_reason
    posting = db.get(JobPosting, row.job_posting_id)
    if posting is not None:
        from ..jobmatch.engine import _recount

        posting.match_count = _recount(db, posting.id)
    return _match_out(db, row)


def _match_out(
    db: Session,
    row: JobMatch,
    *,
    candidate: Candidate | None = None,
) -> JobMatchOut:
    payload = JobMatchOut.model_validate(row)
    person = candidate or db.get(Candidate, row.candidate_id)
    posting = db.get(JobPosting, row.job_posting_id)
    updates: dict = {}
    if person is not None:
        updates["candidate"] = CandidateSummary.model_validate(person)
    if posting is not None:
        updates["posting"] = JobPostingOut.model_validate(posting)
    return payload.model_copy(update=updates) if updates else payload
