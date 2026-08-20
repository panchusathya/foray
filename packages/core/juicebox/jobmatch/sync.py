"""Project ↔ JobPosting upsert. One mandate, one posting."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enrich import taxonomy as tx
from ..jobs import KIND_JOBS_PARSE, enqueue
from ..models import JobPosting, Project, utcnow
from .ids import parse_fingerprint, source_id_for


def upsert_project_posting(db: Session, project: Project) -> JobPosting:
    """Create or refresh the posting for this project.

    Looks up by ``project_id`` first so "create project from this posting"
    (which links an existing Exa/CSV row) does not spawn a clone with
    ``source_id=project:{id}``.
    """
    now = utcnow()
    title = (project.role_title or project.name or "").strip()
    existing = db.scalar(select(JobPosting).where(JobPosting.project_id == project.id))
    if existing is None:
        sid = source_id_for(project_id=project.id)
        existing = db.scalar(
            select(JobPosting).where(
                JobPosting.org_id == project.org_id, JobPosting.source_id == sid
            )
        )
    if existing is None:
        existing = JobPosting(
            org_id=project.org_id,
            source="project",
            source_id=source_id_for(project_id=project.id),
            status="pending_parse",
            first_seen_at=now,
            last_seen_at=now,
            project_id=project.id,
        )
        db.add(existing)

    existing.project_id = project.id
    # A borrowed posting keeps what it came with. "Create project from this
    # posting" links a pre-existing exa/csv row, and a Project carries no
    # description at all unless someone typed one -- so mirroring a blank over
    # the top would delete the scraped JD, the company and the location that
    # the match scoring runs on. Only write what the project actually has.
    if title:
        existing.title = title
        existing.title_normalized = tx.normalize_title(title)
    if project.location:
        existing.location = project.location
    if project.company:
        existing.company = project.company
        existing.company_normalized = tx.normalize_company(project.company)
    if project.description:
        existing.jd_text = project.description
    existing.last_seen_at = now
    if existing.status == "archived":
        existing.status = "pending_parse"
    db.flush()
    enqueue_parse(db, existing)
    return existing


def archive_project_posting(db: Session, project_id: str) -> None:
    """Detach the posting a deleted project owned.

    Only a posting the project *created* is archived. A market posting the
    project merely borrowed (source exa/csv/manual, linked by create-project)
    outlives the mandate: it is still a real open role, still scored against
    everyone, and archiving it would retire it from the market view because a
    client went quiet.
    """
    posting = db.scalar(select(JobPosting).where(JobPosting.project_id == project_id))
    if posting is None:
        return
    posting.project_id = None
    if posting.source == "project":
        posting.status = "archived"


def backfill_project_postings(db: Session, org_id: str | None = None) -> int:
    stmt = select(Project)
    if org_id:
        stmt = stmt.where(Project.org_id == org_id)
    count = 0
    for project in db.scalars(stmt).all():
        upsert_project_posting(db, project)
        count += 1
    return count


def enqueue_parse(db: Session, posting: JobPosting) -> None:
    enqueue(
        db,
        org_id=posting.org_id,
        kind=KIND_JOBS_PARSE,
        idempotency_key=f"{KIND_JOBS_PARSE}:{posting.id}:{parse_fingerprint(posting)}",
        payload={"org_id": posting.org_id, "job_posting_id": posting.id},
    )


def apply_posting_fields(posting: JobPosting, **fields: object) -> None:
    """Fill normalised columns after a write of the raw ones."""
    if "title" in fields and posting.title:
        posting.title_normalized = tx.normalize_title(str(posting.title))
    if "company" in fields:
        posting.company_normalized = tx.normalize_company(str(posting.company or ""))
    if "location" in fields and not posting.location:
        posting.location = ""
