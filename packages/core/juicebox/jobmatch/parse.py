"""jobs.parse handler: spec, fees, warm paths, embedding."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..llm.embeddings import get_embedder
from ..models import Job, JobPosting, utcnow
from .fees import fee_signals
from .ids import spec_version_for
from .referrals import warm_paths
from .spec import job_spec


def parse_posting(db: Session, posting: JobPosting) -> dict:
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
