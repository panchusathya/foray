"""Find jobs FOR one candidate, from what they told us they want.

The recruiter-facing discovery (``discover.py``) is a chat: a human types a
brief and Exa searches it. Nothing did the same thing from the candidate's
side, so a person could finish onboarding -- "senior ML engineer, San
Francisco, 240k" -- and the job book would still only contain whatever a
recruiter had happened to search for that week. The first live thread ended
with 59 postings in the database and zero matched to anybody.

Queries are built in code rather than by a model. The candidate already told
us the titles and the locations in structured form; handing that to an LLM to
turn into a search string adds a way to get it wrong and nothing else.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..jobs import KIND_JOBS_DISCOVER_ONE, KIND_JOBS_PARSE, Job, enqueue
from ..models import Candidate, CandidatePreference, JobPosting, utcnow
from .exa import ExaUnavailable, get_exa
from .scrape import ingest_exa_results

logger = logging.getLogger(__name__)

# Per candidate, per run. Each query is a billed Exa call, and the marginal
# query is the one that returns the same postings as the one before it.
MAX_QUERIES = 3
RESULTS_PER_QUERY = 15


def queries_for(preference: CandidatePreference) -> list[str]:
    """Search strings from the stated spec. Empty when we know too little.

    Deliberately conservative: with no title there is nothing to search for,
    and "jobs" as a query spends money to return noise.
    """
    spec = preference.spec or {}
    titles = [str(t).strip() for t in (spec.get("job_titles") or []) if str(t).strip()]
    if not titles:
        return []

    locations = [str(loc).strip() for loc in (spec.get("locations") or []) if str(loc).strip()]
    seniority = [str(s).strip() for s in (spec.get("seniority") or []) if str(s).strip()]
    level = seniority[0] if seniority else ""
    remote = spec.get("remote_ok") is True

    queries: list[str] = []
    for title in titles[:MAX_QUERIES]:
        parts = [level, title, "jobs"]
        if locations:
            parts.append(f"in {locations[0]}")
        elif remote:
            parts.append("remote")
        query = " ".join(p for p in parts if p)
        if query not in queries:
            queries.append(query)
    return queries[:MAX_QUERIES]


def enqueue_discovery_for_candidate(
    db: Session, *, candidate: Candidate, preference: CandidatePreference
) -> Job | None:
    """Queue one discovery run, keyed so a rescore cannot re-buy the searches."""
    if not queries_for(preference):
        return None
    stamp = int((preference.updated_at or utcnow()).timestamp())
    return enqueue(
        db,
        org_id=candidate.org_id,
        kind=KIND_JOBS_DISCOVER_ONE,
        idempotency_key=f"{KIND_JOBS_DISCOVER_ONE}:{candidate.id}:{stamp}",
        payload={"org_id": candidate.org_id, "candidate_id": candidate.id},
    )


def handle_discover_one(db: Session, job: Job, now) -> dict[str, Any]:
    """Search the open market for this candidate, then rescore them against it."""
    from .engine import enqueue_match_for_candidate

    candidate_id = str(job.payload.get("candidate_id") or "")
    candidate = db.get(Candidate, candidate_id)
    if candidate is None or candidate.org_id != job.org_id:
        return {"skipped": "candidate not found"}

    preference = db.scalar(
        select(CandidatePreference).where(
            CandidatePreference.org_id == candidate.org_id,
            CandidatePreference.candidate_id == candidate_id,
        )
    )
    if preference is None:
        return {"skipped": "no preference"}

    queries = queries_for(preference)
    if not queries:
        return {"skipped": "not enough of a spec to search on"}

    client = get_exa()
    results: list[dict] = []
    ran: list[str] = []
    for query in queries:
        try:
            hits = client.search_job_postings(query, num_results=RESULTS_PER_QUERY)
        except ExaUnavailable as exc:
            # No key: say so once and stop, rather than failing per query.
            logger.warning("candidate discovery skipped, exa unavailable: %s", exc)
            return {"skipped": "exa not configured"}
        except Exception as exc:  # noqa: BLE001 - one bad query must not lose the rest
            logger.warning("candidate discovery query %r failed: %s", query, exc)
            continue
        ran.append(query)
        results.extend(hits)

    summary = ingest_exa_results(db, candidate.org_id, results, now=now)

    # Everything newly ingested needs parsing before it can be matched, and
    # parsing is where the employer and location are read off the page.
    pending = db.scalars(
        select(JobPosting.id).where(
            JobPosting.org_id == candidate.org_id,
            JobPosting.status == "pending_parse",
        )
    ).all()
    for posting_id in pending:
        enqueue(
            db,
            org_id=candidate.org_id,
            kind=KIND_JOBS_PARSE,
            idempotency_key=f"{KIND_JOBS_PARSE}:{posting_id}",
            payload={"org_id": candidate.org_id, "posting_id": posting_id},
        )

    # Rescore last: the matches this candidate cares about are against the
    # postings we just added.
    enqueue_match_for_candidate(db, candidate, preference=preference, now=now)
    return {
        "queries": ran,
        "results": len(results),
        "created": summary.get("created", 0),
        "queued_parse": len(pending),
    }
