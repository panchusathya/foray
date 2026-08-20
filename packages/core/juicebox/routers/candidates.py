"""Candidate ingestion and retrieval."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..enrich.email import guess_emails
from ..models import Candidate, Enrollment, Message, ProjectCandidate, utcnow
from ..outreach.history import (
    candidate_history_stats,
    candidate_timeline,
    contact_rollup_subquery,
)
from ..repository import upsert_candidates
from ..schemas import (
    BulkCandidateIn,
    BulkUpsertResult,
    CandidateIn,
    CandidateOut,
    CandidateSummary,
    CandidateTimelineOut,
    HistoryStatsOut,
    MessageOut,
    ThreadOut,
)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/candidates", tags=["candidates"])

MAX_BULK = 5000
# Above this, "select all matching" stops being a selection and starts
# being a bulk job the client has no way to show progress for.
MAX_SELECT_ALL = 1000

SORT_KEYS = ("name", "last_contacted", "last_reply", "touches", "created")
RELATIONSHIPS = ("stale", "one_touch", "never_replied", "replied")
DEFAULT_STALE_DAYS = 90
# Someone emailed three days ago has not "failed to reply" yet.
NO_REPLY_GRACE_DAYS = 7


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


def _validate_grid_params(scope, sort, direction, relationship):
    if scope not in ("all", "crm"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="scope must be one of: all, crm",
        )
    if sort not in SORT_KEYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"sort must be one of: {', '.join(SORT_KEYS)}",
        )
    if direction not in ("asc", "desc"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="dir must be one of: asc, desc",
        )
    if relationship is not None and relationship not in RELATIONSHIPS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"relationship must be one of: {', '.join(RELATIONSHIPS)}",
        )


def _in_crm_scope():
    """The CRM population, as opposed to the whole sourced pool.

    Deliberately the same three conditions as
    ``integrations.matching.build_address_index``: someone in a project, in a
    sequence, or already in correspondence. When ``crm_tracked`` lands it joins
    this OR here, in one place, so the grid and the mail privacy guard cannot
    drift apart.
    """
    return (
        exists(select(Message.id).where(Message.candidate_id == Candidate.id))
        | exists(
            select(ProjectCandidate.id).where(ProjectCandidate.candidate_id == Candidate.id)
        )
        | exists(select(Enrollment.id).where(Enrollment.candidate_id == Candidate.id))
    )


def _candidate_query(
    *,
    org_id,
    rollup,
    q=None,
    seniority=None,
    scope="all",
    relationship=None,
    stale_days=DEFAULT_STALE_DAYS,
    contacted_before=None,
    contacted_after=None,
    created_after=None,
    now=None,
):
    """One WHERE clause, shared by the listing and the count.

    They used to disagree -- ``/count`` ignored every filter -- so a filtered
    grid had no honest total to show. Building both from here means they cannot.
    """
    now = now or utcnow()
    stmt = (
        select(Candidate)
        .outerjoin(rollup, rollup.c.candidate_id == Candidate.id)
        .where(Candidate.org_id == org_id)
    )

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
    if scope == "crm":
        stmt = stmt.where(_in_crm_scope())

    if contacted_before is not None:
        stmt = stmt.where(rollup.c.last_emailed_at < contacted_before)
    if contacted_after is not None:
        stmt = stmt.where(rollup.c.last_emailed_at >= contacted_after)
    if created_after is not None:
        stmt = stmt.where(Candidate.created_at >= created_after)

    outbound = func.coalesce(rollup.c.outbound_count, 0)
    inbound = func.coalesce(rollup.c.inbound_count, 0)

    if relationship == "replied":
        stmt = stmt.where(inbound > 0)
    elif relationship in ("stale", "one_touch", "never_replied"):
        cutoff = now - timedelta(days=stale_days)
        stmt = stmt.where(outbound >= 1)
        if relationship == "stale":
            # Quiet on both sides: a recent reply means the relationship is
            # alive even if we have not written lately.
            stmt = stmt.where(
                rollup.c.last_emailed_at < cutoff,
                or_(rollup.c.last_reply_at.is_(None), rollup.c.last_reply_at < cutoff),
            )
        else:
            stmt = stmt.where(inbound == 0)
            if relationship == "one_touch":
                stmt = stmt.where(outbound == 1)
            # Let a fresh send land before calling it a non-reply.
            stmt = stmt.where(
                rollup.c.last_emailed_at < now - timedelta(days=NO_REPLY_GRACE_DAYS)
            )
        # Mid-sequence people are already being chased; surfacing them for
        # revival would double-touch them.
        stmt = stmt.where(
            ~exists(
                select(Enrollment.id).where(
                    Enrollment.candidate_id == Candidate.id,
                    Enrollment.status == "active",
                )
            )
        )
    return stmt


def _ordering(rollup, sort, direction):
    """Date sorts are NULLS LAST in *both* directions on purpose: people who
    were never contacted are not the answer to "who did I contact most
    recently", and they are not the answer to "longest ago" either."""
    descending = direction == "desc"
    if sort == "name":
        column = Candidate.full_name
    elif sort == "created":
        column = Candidate.created_at
    elif sort == "last_contacted":
        column = rollup.c.last_emailed_at
    elif sort == "last_reply":
        column = rollup.c.last_reply_at
    else:  # touches
        column = func.coalesce(rollup.c.outbound_count, 0)

    ordered = column.desc() if descending else column.asc()
    if sort in ("last_contacted", "last_reply"):
        ordered = ordered.nulls_last()
    # Stable paging: without a unique tiebreak, rows with equal keys can
    # reshuffle between pages and one shows up twice or not at all.
    return (ordered, Candidate.id.asc())


@router.get("", response_model=list[CandidateSummary])
def list_candidates(
    q: str | None = Query(default=None, description="Substring match on name, title or company"),
    skill: list[str] | None = Query(default=None),
    seniority: list[str] | None = Query(default=None),
    scope: str = Query(default="all", description="all (sourced pool) | crm"),
    sort: str = Query(default="name"),
    dir: str = Query(default="asc", description="asc | desc"),
    relationship: str | None = Query(default=None),
    stale_days: int = Query(default=DEFAULT_STALE_DAYS, ge=1, le=3650),
    contacted_before: datetime | None = Query(default=None),
    contacted_after: datetime | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_history: bool = Query(
        default=False, description="Attach first/last-contact stats"
    ),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[CandidateSummary]:
    """The people list, and the CRM grid behind it.

    Every new parameter is optional and the defaults reproduce the original
    behaviour exactly -- the sequence editor's candidate picker calls this with
    nothing but ``q`` and ``limit`` and must keep seeing the whole org as a
    bare array.
    """
    _validate_grid_params(scope, sort, dir, relationship)
    rollup = contact_rollup_subquery(principal.org_id)
    stmt = _candidate_query(
        org_id=principal.org_id,
        rollup=rollup,
        q=q,
        seniority=seniority,
        scope=scope,
        relationship=relationship,
        stale_days=stale_days,
        contacted_before=contacted_before,
        contacted_after=contacted_after,
        created_after=created_after,
    )
    rows = db.scalars(
        stmt.order_by(*_ordering(rollup, sort, dir)).limit(limit).offset(offset)
    ).all()

    if skill:
        # Still filtered in Python, still after LIMIT/OFFSET, so still wrong
        # when paged. Contacts does not send it; the fix needs a skills
        # projection table and lands with it.
        wanted = {s.lower() for s in skill}
        rows = [r for r in rows if wanted & {s.lower() for s in (r.skills or [])}]

    summaries = [CandidateSummary.model_validate(r) for r in rows]
    if include_history and summaries:
        stats = candidate_history_stats(
            db, org_id=principal.org_id, candidate_ids=[s.id for s in summaries]
        )
        for summary in summaries:
            found = stats.get(summary.id)
            if found is not None:
                summary.history = HistoryStatsOut(**found.__dict__)
    return summaries


@router.get("/count")
def count_candidates(
    q: str | None = Query(default=None),
    seniority: list[str] | None = Query(default=None),
    scope: str = Query(default="all"),
    relationship: str | None = Query(default=None),
    stale_days: int = Query(default=DEFAULT_STALE_DAYS, ge=1, le=3650),
    contacted_before: datetime | None = Query(default=None),
    contacted_after: datetime | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """The total for the *same* filters the listing ran.

    ``skill`` is absent by design: it is applied in Python after paging, so a
    SQL count could not agree with the listing, and a confidently wrong total
    is worse than no total.
    """
    _validate_grid_params(scope, "name", "asc", relationship)
    rollup = contact_rollup_subquery(principal.org_id)
    stmt = _candidate_query(
        org_id=principal.org_id,
        rollup=rollup,
        q=q,
        seniority=seniority,
        scope=scope,
        relationship=relationship,
        stale_days=stale_days,
        contacted_before=contacted_before,
        contacted_after=contacted_after,
        created_after=created_after,
    )
    total = db.scalar(
        select(func.count()).select_from(stmt.with_only_columns(Candidate.id).subquery())
    )
    return {"count": int(total or 0)}


@router.get("/ids")
def candidate_ids(
    q: str | None = Query(default=None),
    seniority: list[str] | None = Query(default=None),
    scope: str = Query(default="all"),
    relationship: str | None = Query(default=None),
    stale_days: int = Query(default=DEFAULT_STALE_DAYS, ge=1, le=3650),
    contacted_before: datetime | None = Query(default=None),
    contacted_after: datetime | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=MAX_SELECT_ALL),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict:
    """Ids for "select everything matching", capped.

    Ids only, deliberately: the caller is building a selection, not a table,
    and returning full rows would tempt a client into exporting from here --
    which would quietly export a different column set than the grid shows.

    ``truncated`` is what stops the UI claiming it selected everything when it
    selected the first thousand.
    """
    _validate_grid_params(scope, "name", "asc", relationship)
    rollup = contact_rollup_subquery(principal.org_id)
    stmt = _candidate_query(
        org_id=principal.org_id,
        rollup=rollup,
        q=q,
        seniority=seniority,
        scope=scope,
        relationship=relationship,
        stale_days=stale_days,
        contacted_before=contacted_before,
        contacted_after=contacted_after,
        created_after=created_after,
    )
    rows = db.scalars(
        stmt.order_by(*_ordering(rollup, "name", "asc")).limit(limit + 1)
    ).all()
    ids = [row.id for row in rows[:limit]]
    return {"ids": ids, "truncated": len(rows) > limit}


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


@router.get("/{candidate_id}/timeline", response_model=CandidateTimelineOut)
def get_candidate_timeline(
    candidate_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> CandidateTimelineOut:
    """Contact history: the stats header plus messages grouped into threads,
    in one round trip — sequence sends and synced Outlook mail together."""
    candidate = db.scalar(
        select(Candidate).where(
            Candidate.id == candidate_id, Candidate.org_id == principal.org_id
        )
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    stats, threads = candidate_timeline(
        db, org_id=principal.org_id, candidate_id=candidate_id, limit=limit
    )
    return CandidateTimelineOut(
        stats=HistoryStatsOut(**stats.__dict__),
        threads=[
            ThreadOut(
                conversation_id=thread.conversation_id,
                subject=thread.subject,
                last_at=thread.last_at,
                messages=[MessageOut.model_validate(m) for m in thread.messages],
            )
            for thread in threads
        ],
    )


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
