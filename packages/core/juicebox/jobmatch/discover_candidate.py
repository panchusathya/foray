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
from sqlalchemy.orm.attributes import flag_modified

from ..jobs import KIND_JOBS_DISCOVER_ONE, OPEN_STATUSES, Job, enqueue
from ..models import Candidate, CandidatePreference, JobPosting, utcnow
from .exa import ExaUnavailable, get_exa
from .scrape import ingest_exa_results
from .sync import enqueue_parse

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


def queries_for_request(
    preference: CandidatePreference | None = None,
    *,
    query: str = "",
    location: str = "",
) -> list[str]:
    """Preference queries plus an explicit ask from Foray Instinct or chat.

    A typed "rust jobs in NYC" is the search they asked for. The stored spec
    is what we already knew. Both go to Exa; neither is invented here.
    """
    queries = queries_for(preference) if preference is not None else []
    focus = query.strip()
    if not focus:
        return queries
    extra = f"{focus} jobs"
    where = location.strip()
    if where:
        extra = f"{extra} in {where}"
    if extra not in queries:
        return [extra, *queries][:MAX_QUERIES]
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


def enqueue_discovery_on_request(
    db: Session,
    *,
    candidate: Candidate,
    preference: CandidatePreference | None = None,
    now=None,
    query: str = "",
    location: str = "",
) -> Job | None:
    """Go and look now, because the candidate just asked us to.

    Distinct from ``enqueue_discovery_for_candidate``, which keys on
    ``preference.updated_at`` so that re-scoring an unchanged spec cannot
    re-buy the same searches. That key is right for the automatic path and
    wrong for this one: somebody typing "find me roles" a week later has an
    unchanged spec, so the automatic key dedupes their request against a run
    that already happened and nothing is searched.

    Keyed per candidate per day instead. Asking twice in an afternoon costs
    nothing, and the cap is what keeps a bored candidate from spending the
    Exa budget one text at a time.

    ``query`` / ``location`` are the words Foray Instinct (or chat) just
    handed us. They ride on the job payload so Exa searches what was asked,
    not only what intake stored last week.
    """
    queries = queries_for_request(preference, query=query, location=location)
    if not queries:
        return None

    # A search already waiting to run is the search they just asked for. The
    # automatic enqueue keys on `preference.updated_at` and this one keys on
    # the day, so the same turn can produce both -- and both would buy the same
    # queries twice. Reuse whichever is already pending, but fold in any new
    # explicit query so "rust jobs" is not swallowed by a preference-only wait.
    pending = db.scalar(
        select(Job).where(
            Job.org_id == candidate.org_id,
            Job.kind == KIND_JOBS_DISCOVER_ONE,
            Job.status.in_(OPEN_STATUSES),
        )
    )
    if pending is not None and (pending.payload or {}).get("candidate_id") == candidate.id:
        existing = [
            str(item).strip()
            for item in (pending.payload or {}).get("queries") or []
            if str(item).strip()
        ]
        merged = list(dict.fromkeys([*existing, *queries]))
        pending.payload = {**(pending.payload or {}), "queries": merged}
        flag_modified(pending, "payload")
        return pending

    day = (now or utcnow()).strftime("%Y%m%d")
    return enqueue(
        db,
        org_id=candidate.org_id,
        kind=KIND_JOBS_DISCOVER_ONE,
        idempotency_key=f"{KIND_JOBS_DISCOVER_ONE}:req:{candidate.id}:{day}",
        payload={
            "org_id": candidate.org_id,
            "candidate_id": candidate.id,
            "queries": queries,
        },
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
    queries = [
        str(item).strip()
        for item in (job.payload.get("queries") or [])
        if str(item).strip()
    ] or (queries_for(preference) if preference is not None else [])
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
    #
    # Via ``enqueue_parse``, not a hand-rolled ``enqueue``: this loop used to
    # write ``payload={"posting_id": ...}`` while ``jobmatch.parse.handle_parse``
    # reads ``job_posting_id``, so every job it queued returned
    # "missing_posting" and did nothing. The count came back non-zero anyway,
    # which is what made the failure invisible.
    pending = db.scalars(
        select(JobPosting).where(
            JobPosting.org_id == candidate.org_id,
            JobPosting.status == "pending_parse",
        )
    ).all()
    for posting in pending:
        enqueue_parse(db, posting)

    # Rescore last: the matches this candidate cares about are against the
    # postings we just added. A Foray ask can search before intake has a
    # preference row; ingest still runs, matching waits for a spec.
    if preference is not None:
        enqueue_match_for_candidate(db, candidate, preference=preference, now=now)
    return {
        "queries": ran,
        "results": len(results),
        "created": summary.get("created", 0),
        "queued_parse": len(pending),
    }
