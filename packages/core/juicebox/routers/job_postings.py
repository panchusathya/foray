"""Job postings: intake, listing, parse, rematch, create-project-from-posting."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..enrich import taxonomy as tx
from ..jobmatch.engine import match_posting
from ..jobmatch.ids import source_id_for
from ..jobmatch.sync import enqueue_parse
from ..models import Candidate, JobMatch, JobPosting, Project, utcnow
from ..schemas import (
    CandidateSummary,
    CreateProjectFromPosting,
    JobMatchOut,
    JobPostingImport,
    JobPostingIn,
    JobPostingOut,
    JobPostingPatch,
    ProjectOut,
)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/job-postings", tags=["job-postings"])

ALLOWED_STATUS = {"pending_parse", "open", "stale", "closed", "archived"}


def _load_posting(db: Session, posting_id: str, org_id: str) -> JobPosting:
    posting = db.scalar(
        select(JobPosting).where(JobPosting.id == posting_id, JobPosting.org_id == org_id)
    )
    if posting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found")
    return posting


def _fill(posting: JobPosting, payload: JobPostingIn | dict) -> None:
    data = payload.model_dump() if hasattr(payload, "model_dump") else payload
    for key in (
        "title",
        "company",
        "location",
        "jd_text",
        "source_url",
        "seniority",
        "open_to_remote",
        "project_id",
    ):
        if key in data and data[key] is not None:
            setattr(posting, key, data[key])
    posting.title_normalized = tx.normalize_title(posting.title or "")
    posting.company_normalized = tx.normalize_company(posting.company or "")


@router.post("", response_model=JobPostingOut, status_code=status.HTTP_201_CREATED)
def create_posting(
    payload: JobPostingIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> JobPostingOut:
    now = utcnow()
    sid = source_id_for(
        url=payload.source_url,
        company=payload.company,
        title=payload.title,
        location=payload.location,
    )
    existing = db.scalar(
        select(JobPosting).where(
            JobPosting.org_id == principal.org_id, JobPosting.source_id == sid
        )
    )
    if existing is not None:
        _fill(existing, payload)
        existing.last_seen_at = now
        enqueue_parse(db, existing)
        return JobPostingOut.model_validate(existing)

    posting = JobPosting(
        org_id=principal.org_id,
        source="manual",
        source_id=sid,
        status="pending_parse",
        first_seen_at=now,
        last_seen_at=now,
    )
    _fill(posting, payload)
    db.add(posting)
    db.flush()
    enqueue_parse(db, posting)
    return JobPostingOut.model_validate(posting)


@router.post("/import", response_model=dict)
def import_postings(
    payload: JobPostingImport,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    now = utcnow()
    source = payload.source if payload.source in {"csv", "manual", "exa"} else "csv"
    created = 0
    updated = 0
    ids: list[str] = []
    for item in payload.postings:
        sid = source_id_for(
            url=item.source_url,
            company=item.company,
            title=item.title,
            location=item.location,
        )
        posting = db.scalar(
            select(JobPosting).where(
                JobPosting.org_id == principal.org_id, JobPosting.source_id == sid
            )
        )
        if posting is None:
            posting = JobPosting(
                org_id=principal.org_id,
                source=source,
                source_id=sid,
                status="pending_parse",
                first_seen_at=now,
            )
            db.add(posting)
            created += 1
        else:
            updated += 1
        _fill(posting, item)
        posting.last_seen_at = now
        db.flush()
        enqueue_parse(db, posting)
        ids.append(posting.id)
    return {"created": created, "updated": updated, "ids": ids}


@router.get("", response_model=dict)
def list_postings(
    q: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    fee_bearing: bool | None = Query(default=None),
    has_warm_path: bool | None = Query(default=None),
    company: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(JobPosting).where(JobPosting.org_id == principal.org_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                JobPosting.title.ilike(like),
                JobPosting.company.ilike(like),
                JobPosting.location.ilike(like),
            )
        )
    if status_filter:
        stmt = stmt.where(JobPosting.status == status_filter)
    if fee_bearing is not None:
        stmt = stmt.where(JobPosting.fee_bearing.is_(fee_bearing))
    if has_warm_path is not None:
        stmt = stmt.where(JobPosting.has_warm_path.is_(has_warm_path))
    if company:
        stmt = stmt.where(JobPosting.company_normalized == tx.normalize_company(company))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(JobPosting.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return {
        "items": [JobPostingOut.model_validate(row) for row in rows],
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{posting_id}", response_model=JobPostingOut)
def get_posting(
    posting_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> JobPostingOut:
    return JobPostingOut.model_validate(_load_posting(db, posting_id, principal.org_id))


@router.patch("/{posting_id}", response_model=JobPostingOut)
def patch_posting(
    posting_id: str,
    payload: JobPostingPatch,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> JobPostingOut:
    posting = _load_posting(db, posting_id, principal.org_id)
    data = payload.model_dump(exclude_unset=True)
    if "status" in data:
        if data["status"] not in ALLOWED_STATUS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"status must be one of {sorted(ALLOWED_STATUS)}",
            )
        posting.status = data.pop("status")
    _fill(posting, data)
    db.flush()
    if any(key in data for key in ("title", "company", "location", "jd_text", "seniority")):
        enqueue_parse(db, posting)
    return JobPostingOut.model_validate(posting)


@router.get("/{posting_id}/matches", response_model=list[JobMatchOut])
def list_posting_matches(
    posting_id: str,
    min_score: float | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[JobMatchOut]:
    posting = _load_posting(db, posting_id, principal.org_id)
    floor = min_score if min_score is not None else get_settings().job_match_min_score
    rows = db.scalars(
        select(JobMatch)
        .where(
            JobMatch.job_posting_id == posting.id,
            JobMatch.status != "dismissed",
            JobMatch.score >= floor,
        )
        .order_by(JobMatch.score.desc())
    ).all()
    candidate_ids = [row.candidate_id for row in rows]
    people = {
        c.id: c
        for c in db.scalars(
            select(Candidate).where(Candidate.id.in_(candidate_ids or ["__none__"]))
        ).all()
    }
    out: list[JobMatchOut] = []
    for row in rows:
        payload = JobMatchOut.model_validate(row)
        cand = people.get(row.candidate_id)
        if cand is not None:
            payload = payload.model_copy(
                update={"candidate": CandidateSummary.model_validate(cand)}
            )
        out.append(payload)
    return out


@router.post("/{posting_id}/rematch", response_model=dict)
def rematch_posting(
    posting_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    posting = _load_posting(db, posting_id, principal.org_id)
    result = match_posting(db, posting)
    return result


@router.post("/{posting_id}/create-project", response_model=ProjectOut)
def create_project_from_posting(
    posting_id: str,
    payload: CreateProjectFromPosting,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> ProjectOut:
    """Link this posting to a new Project without cloning a second posting."""
    posting = _load_posting(db, posting_id, principal.org_id)
    if posting.project_id:
        project = db.get(Project, posting.project_id)
        if project is not None:
            return ProjectOut.model_validate(project)
    name = (payload.name or posting.title or "Role").strip()
    project = Project(
        org_id=principal.org_id,
        created_by=principal.user_id,
        name=name,
        role_title=posting.title or name,
        description=posting.jd_text or "",
        location=posting.location or "",
        company=posting.company or "",
    )
    db.add(project)
    db.flush()
    posting.project_id = project.id
    return ProjectOut.model_validate(project)
