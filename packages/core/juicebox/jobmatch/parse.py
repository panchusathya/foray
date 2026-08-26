"""jobs.parse handler: spec, fees, warm paths, embedding."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..llm.embeddings import get_embedder
from ..models import Job, JobPosting, utcnow
from .fees import fee_signals
from .ids import spec_version_for
from .referrals import warm_paths
from .scrape import LOCATION_MAX, TITLE_MAX
from .spec import job_spec

logger = logging.getLogger(__name__)


def resolve_identity(posting: JobPosting) -> str:
    """Fill in title/company/location, and reject pages that are not one job.

    Exa's search results carry neither: ``company`` was being read from
    ``author`` (empty for every job board) and ``location`` from a field the
    search API does not return at all, so both were structurally always blank
    and the spec parser -- which only *reads* those columns -- had nothing to
    work with. A card with no employer and no location is not a card.

    Returns the reason the posting was rejected, or "" when it stands. Only
    called when something is actually missing, so a hand-entered posting with
    a real company costs nothing.
    """
    from ..llm.client import get_llm
    from ..llm.prompts import (
        POSTING_IDENTITY_SCHEMA,
        POSTING_IDENTITY_SYSTEM,
        posting_identity_prompt,
    )

    text = (posting.jd_text or "").strip()
    if not text:
        return ""
    llm = get_llm()
    if not llm.available:
        # No key: keep whatever the scrape gave us rather than dropping the
        # row. A thin posting is worth more than no posting.
        return ""
    try:
        result = llm.json_fast(
            system=POSTING_IDENTITY_SYSTEM,
            prompt=posting_identity_prompt(
                title=posting.title or "", url=posting.source_url or "", jd_text=text
            ),
            schema=POSTING_IDENTITY_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 - degrade, never fail a parse
        logger.warning("posting identity extraction failed for %s: %s", posting.id, exc)
        return ""

    data = result.data or {}
    if data.get("is_posting") is False:
        return "not a single job posting"

    # Only ever fill blanks. A company somebody typed in beats one a model read.
    title = str(data.get("title") or "").strip()
    company = str(data.get("company") or "").strip()
    location = str(data.get("location") or "").strip()
    # The scrape path's limits, not ad-hoc ones: title[:300] into a String(240)
    # is rejected outright by Postgres, so a model echoing a long page heading
    # failed the whole jobs.parse job and the posting never left pending_parse.
    if title and (not posting.title or _is_site_furniture(posting.title)):
        posting.title = title[:TITLE_MAX].rstrip()
    if company and not (posting.company or "").strip():
        posting.company = company[:TITLE_MAX].rstrip()
    if location and not (posting.location or "").strip():
        posting.location = location[:LOCATION_MAX].rstrip()
    return ""


# "Founding Engineer at Ventura | Y Combinator" -- a page title, not a role.
_FURNITURE = (" | ", " - ", " at ", " @ ")


def _is_site_furniture(title: str) -> bool:
    return any(token in title for token in _FURNITURE)


def parse_posting(db: Session, posting: JobPosting) -> dict:
    if not (posting.company or "").strip() or not (posting.location or "").strip():
        rejected = resolve_identity(posting)
        if rejected:
            # Closed, not deleted: the row records that we saw the URL and
            # decided against it, so the next sweep does not re-ingest it.
            posting.status = "closed"
            posting.parsed_at = utcnow()
            db.flush()
            logger.info("posting %s rejected: %s", posting.id, rejected)
            return {"parser": "identity", "status": posting.status, "rejected": rejected}

    spec, parser = job_spec(posting)
    payload = spec.to_dict()
    posting.parsed_spec = payload
    posting.spec_version = spec_version_for(payload)
    posting.parser = parser
    posting.parsed_at = utcnow()
    if spec.seniority and not posting.seniority:
        posting.seniority = spec.seniority[0]
    if spec.remote_ok is True:
        posting.open_to_remote = True

    # Where an application actually goes, from the URL alone -- this runs on
    # every posting we ingest, so a fetch per posting is a fetch per posting. It
    # is also what stops a candidate being handed a LinkedIn listing as their
    # "application link", which was true before any automation existed.
    from ..applications.ats.detect import resolve_target

    try:
        resolve_target(db, posting, allow_network=False)
    except Exception as exc:  # noqa: BLE001 - a posting parses fine without it
        logger.info("could not resolve an apply target for %s: %s", posting.id, exc)

    bearing, terms, evidence = fee_signals(posting.jd_text or "")
    posting.fee_bearing = bearing
    posting.fee_terms = terms
    posting.fee_evidence = evidence

    paths = warm_paths(db, posting.org_id, posting)
    posting.referral_paths = paths
    posting.has_warm_path = bool(paths)

    embedder = get_embedder()
    text = spec.retrieval_text() or posting.title or posting.jd_text or ""
    if text.strip():
        vector = embedder.embed([text], input_type="document")[0]
        posting.embedding = [float(v) for v in vector]
        posting.embedding_model = embedder.name
    else:
        posting.embedding = None
        posting.embedding_model = embedder.name

    if posting.status == "pending_parse":
        posting.status = "open"
    db.flush()
    if posting.status == "open":
        from .engine import enqueue_match_for_posting

        enqueue_match_for_posting(db, posting)
    return {"parser": parser, "spec_version": posting.spec_version, "status": posting.status}


def handle_parse(db: Session, job: Job, now) -> dict:
    del now
    posting_id = (job.payload or {}).get("job_posting_id")
    posting = db.get(JobPosting, posting_id) if posting_id else None
    if posting is None:
        return {"skipped": "missing_posting"}
    return parse_posting(db, posting)
