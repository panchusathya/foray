"""jobs.scrape: pull postings from Exa on a cadence, then parse."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..enrich import taxonomy as tx
from ..jobs import KIND_JOBS_SCRAPE_ONE, enqueue
from ..models import Job, JobPosting, JobScrapeQuery, Project, utcnow
from .exa import ExaRequestFailed, ExaUnavailable, get_exa
from .ids import source_id_for
from .sync import enqueue_parse

logger = logging.getLogger(__name__)


def is_due(query: JobScrapeQuery, now=None) -> bool:
    if query.status != "active":
        return False
    now = now or utcnow()
    if query.last_run_at is None:
        return True
    last = query.last_run_at
    if last.tzinfo is None:
        from datetime import UTC

        last = last.replace(tzinfo=UTC)
    return now - last >= timedelta(minutes=max(query.cadence_minutes, 1))


def mark_stale_postings(db: Session, org_id: str, *, now=None) -> int:
    now = now or utcnow()
    settings = get_settings()
    cutoff = now - timedelta(days=max(settings.job_posting_stale_days, 1))
    stale = 0
    rows = db.scalars(
        select(JobPosting).where(
            JobPosting.org_id == org_id,
            JobPosting.status == "open",
            JobPosting.source == "exa",
            # A posting attached to a live mandate is not market noise. Linking
            # via create-project leaves source="exa", so without this the sweep
            # retires the recruiter's own open role and drops it out of
            # matching.
            JobPosting.project_id.is_(None),
        )
    ).all()
    for posting in rows:
        seen = posting.last_seen_at
        if seen is None:
            continue
        if seen.tzinfo is None:
            from datetime import UTC

            seen = seen.replace(tzinfo=UTC)
        if seen < cutoff:
            posting.status = "stale"
            stale += 1
    if stale:
        db.flush()
    return stale


#: ``job_postings.title``/``company`` are String(240), ``location`` String(200).
#: Exa returns whatever the page's <title> and byline happen to be, and Postgres
#: rejects an over-length value rather than truncating it, so an unusually long
#: title would fail the whole ingest -- invisibly on SQLite, where the tests run.
TITLE_MAX = 240
LOCATION_MAX = 200


def _fit(value: str, limit: int) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[:limit].rstrip()


def ingest_exa_results(
    db: Session, org_id: str, results: list[dict], *, now=None
) -> dict:
    now = now or utcnow()
    created = 0
    updated = 0
    for raw in results:
        url = (raw.get("url") or "").strip()
        title = _fit(raw.get("title") or "", TITLE_MAX)
        text = (raw.get("text") or raw.get("summary") or "").strip()
        if not title and not text:
            continue
        sid = source_id_for(url=url, title=title, company=raw.get("author") or "")
        posting = db.scalar(
            select(JobPosting).where(JobPosting.org_id == org_id, JobPosting.source_id == sid)
        )
        if posting is None:
            posting = JobPosting(
                org_id=org_id,
                source="exa",
                source_id=sid,
                status="pending_parse",
                first_seen_at=now,
            )
            db.add(posting)
            created += 1
        else:
            updated += 1
        posting.source_url = url
        # A posting linked to a project is the mandate's own record: whatever a
        # recruiter typed there outranks what the scraper reads off the page, so
        # a re-scrape refreshes liveness and provenance but not the content.
        if posting.project_id is None:
            posting.title = title or posting.title
            posting.title_normalized = tx.normalize_title(posting.title)
            posting.jd_text = text or posting.jd_text
        # Not `author`: on a job board that is the board, or empty. The
        # employer is read out of the page text by `parse.resolve_identity`.
        posting.company = posting.company or ""
        posting.company_normalized = _fit(
            tx.normalize_company(posting.company), TITLE_MAX
        )
        posting.last_seen_at = now
        posting.raw = raw
        if posting.status == "stale":
            posting.status = "pending_parse"
        db.flush()
        enqueue_parse(db, posting)
    return {"created": created, "updated": updated}


def ensure_auto_queries(db: Session, org_id: str) -> int:
    """One saved query per normalised title family, capped per org."""
    settings = get_settings()
    cap = max(settings.job_scrape_max_auto_queries, 0)
    existing = list(
        db.scalars(select(JobScrapeQuery).where(JobScrapeQuery.org_id == org_id)).all()
    )
    auto = [q for q in existing if q.origin == "auto"]
    if cap == 0:
        return 0
    titles: list[str] = []
    for project in db.scalars(select(Project).where(Project.org_id == org_id)).all():
        titles.append(tx.normalize_title(project.role_title or project.name))
    for posting in db.scalars(
        select(JobPosting).where(JobPosting.org_id == org_id, JobPosting.status == "open")
    ).all():
        titles.append(posting.title_normalized or tx.normalize_title(posting.title))
    families: list[str] = []
    seen: set[str] = set()
    for title in titles:
        family = title.strip()
        if not family or family in seen:
            continue
        seen.add(family)
        families.append(family)
        if len(families) >= cap:
            break
    have = {q.query for q in auto}
    created = 0
    for family in families:
        # Tested before the insert, not after: checking afterwards lets an org
        # already at the cap add one more every tick, so the ceiling never
        # actually holds and each extra query is a recurring billed search.
        if len(auto) + created >= cap:
            break
        query_text = f"{family} job posting"
        if query_text in have:
            continue
        db.add(
            JobScrapeQuery(
                org_id=org_id,
                query=query_text,
                cadence_minutes=1440,
                status="active",
                origin="auto",
            )
        )
        created += 1
    if created:
        db.flush()
    return created


def run_scrape_query(db: Session, query: JobScrapeQuery, *, now=None) -> dict:
    now = now or utcnow()
    client = get_exa()
    if not client.available:
        query.last_run_at = now
        return {"skipped": "no_exa_key"}
    # last_run_at is stamped on every outcome, success or not. is_due() treats
    # a null as "never ran, run now", so leaving it unset after a failed call
    # makes the 60s tick re-enqueue the query forever -- and every attempt is a
    # billed Exa search, retried five times by the job runner. An attempt that
    # failed still counts as an attempt; the cadence backs off and the next run
    # tries again on schedule.
    try:
        results = client.search_job_postings(query.query)
    except ExaUnavailable:
        query.last_run_at = now
        return {"skipped": "no_exa_key"}
    except ExaRequestFailed as exc:
        query.last_run_at = now
        db.flush()
        logger.warning("exa search failed for query %s: %s", query.id, exc)
        return {"skipped": "exa_request_failed", "error": str(exc)}
    ingested = ingest_exa_results(db, query.org_id, results, now=now)
    query.last_run_at = now
    query.total_found = int(query.total_found or 0) + ingested["created"]
    db.flush()
    return ingested


def dispatch_scrape_tick(db: Session, org_id: str, *, now=None) -> dict:
    now = now or utcnow()
    ensure_auto_queries(db, org_id)
    stale = mark_stale_postings(db, org_id, now=now)
    queued = 0
    for query in db.scalars(
        select(JobScrapeQuery).where(JobScrapeQuery.org_id == org_id)
    ).all():
        if not is_due(query, now=now):
            continue
        enqueue(
            db,
            org_id=org_id,
            kind=KIND_JOBS_SCRAPE_ONE,
            idempotency_key=f"{KIND_JOBS_SCRAPE_ONE}:{query.id}:{int(now.timestamp())}",
            payload={"org_id": org_id, "query_id": query.id},
            run_at=now,
        )
        queued += 1
    return {"queued": queued, "stale": stale}


def handle_scrape_one(db: Session, job: Job, now) -> dict:
    query = db.get(JobScrapeQuery, (job.payload or {}).get("query_id", ""))
    if query is None:
        return {"skipped": "missing_query"}
    client = get_exa()
    if not client.available:
        query.last_run_at = now
        return {"skipped": "no_exa_key"}
    return run_scrape_query(db, query, now=now)
