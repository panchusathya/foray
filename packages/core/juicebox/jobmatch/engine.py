"""One scorer writes JobMatch. Both directions call ``score_pair``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..jobs import KIND_JOBS_MATCH_ONE, enqueue
from ..models import (
    Candidate,
    Job,
    JobMatch,
    JobMatchWatermark,
    JobPosting,
    Suppression,
    utcnow,
)
from ..outreach.suppressions import normalize_email
from ..search.index import candidate_to_doc
from ..search.parser import ParsedQuery
from ..search.scoring import assess
from .referrals import is_incumbent, warm_paths


@dataclass
class PairScore:
    score: float
    heuristic_score: float
    vector_score: float
    criteria: list[dict]


def score_pair(
    doc,
    spec: ParsedQuery,
    *,
    cand_embedding: list[float] | None,
    cand_embedding_model: str,
    posting_embedding: list[float] | None,
    posting_embedding_model: str,
) -> PairScore:
    assessment = assess(doc, spec)
    heuristic = float(assessment.score)
    vector = _vector_term(
        cand_embedding,
        cand_embedding_model,
        posting_embedding,
        posting_embedding_model,
    )
    settings = get_settings()
    hw = settings.job_match_heuristic_weight
    vw = settings.job_match_vector_weight
    if vector is None:
        blended = heuristic
        vector_score = 0.0
    else:
        weight_sum = hw + vw
        blended = heuristic if weight_sum <= 0 else (hw * heuristic + vw * vector) / weight_sum
        vector_score = vector
    return PairScore(
        score=max(0.0, min(1.0, blended)),
        heuristic_score=heuristic,
        vector_score=vector_score,
        criteria=[c.to_dict() for c in assessment.criteria],
    )


def _vector_term(
    cand_embedding: list[float] | None,
    cand_model: str,
    posting_embedding: list[float] | None,
    posting_model: str,
) -> float | None:
    if not cand_embedding or not posting_embedding:
        return None
    if not cand_model or not posting_model or cand_model != posting_model:
        return None
    if len(cand_embedding) != len(posting_embedding):
        return None
    left = np.asarray(cand_embedding, dtype=np.float32)
    right = np.asarray(posting_embedding, dtype=np.float32)
    norms = float(np.linalg.norm(left)), float(np.linalg.norm(right))
    if norms[0] == 0 or norms[1] == 0:
        return None
    cosine = float(left @ right / (norms[0] * norms[1]))
    return max(0.0, min(1.0, cosine))


def match_posting(db: Session, posting: JobPosting, *, now=None) -> dict:
    now = now or utcnow()
    if posting.status != "open":
        posting.matched_at = now
        posting.matched_spec_version = posting.spec_version or ""
        return {"skipped": "not_open", "matched": 0}
    spec = ParsedQuery.from_dict(posting.parsed_spec or {})
    suppressed = _suppression_set(db, posting.org_id)
    candidates = db.scalars(
        select(Candidate)
        .where(Candidate.org_id == posting.org_id)
        .options(selectinload(Candidate.experiences), selectinload(Candidate.educations))
    ).all()
    kept = 0
    for candidate in candidates:
        if _is_suppressed_candidate(candidate, suppressed):
            _touch_watermark(db, posting.org_id, candidate.id, now)
            continue
        if is_incumbent(candidate, posting):
            _drop_match(db, posting.id, candidate.id)
            _touch_watermark(db, posting.org_id, candidate.id, now)
            continue
        pair = score_pair(
            candidate_to_doc(candidate),
            spec,
            cand_embedding=candidate.embedding,
            cand_embedding_model=candidate.embedding_model or "",
            posting_embedding=posting.embedding,
            posting_embedding_model=posting.embedding_model or "",
        )
        _upsert_match(db, posting, candidate.id, pair, now=now)
        _touch_watermark(db, posting.org_id, candidate.id, now)
        kept += 1
    posting.referral_paths = warm_paths(db, posting.org_id, posting)
    posting.has_warm_path = bool(posting.referral_paths)
    posting.matched_at = now
    posting.matched_spec_version = posting.spec_version or ""
    posting.match_count = _recount(db, posting.id)
    db.flush()
    return {"matched": kept, "match_count": posting.match_count}


def match_candidate(db: Session, candidate: Candidate, *, now=None) -> dict:
    now = now or utcnow()
    candidate = db.scalar(
        select(Candidate)
        .where(Candidate.id == candidate.id)
        .options(selectinload(Candidate.experiences), selectinload(Candidate.educations))
    ) or candidate
    suppressed = _suppression_set(db, candidate.org_id)
    if _is_suppressed_candidate(candidate, suppressed):
        _touch_watermark(db, candidate.org_id, candidate.id, now)
        return {"skipped": "suppressed", "matched": 0}
    postings = db.scalars(
        select(JobPosting).where(
            JobPosting.org_id == candidate.org_id, JobPosting.status == "open"
        )
    ).all()
    doc = candidate_to_doc(candidate)
    kept = 0
    for posting in postings:
        if is_incumbent(candidate, posting):
            _drop_match(db, posting.id, candidate.id)
            continue
        spec = ParsedQuery.from_dict(posting.parsed_spec or {})
        pair = score_pair(
            doc,
            spec,
            cand_embedding=candidate.embedding,
            cand_embedding_model=candidate.embedding_model or "",
            posting_embedding=posting.embedding,
            posting_embedding_model=posting.embedding_model or "",
        )
        _upsert_match(db, posting, candidate.id, pair, now=now)
        posting.match_count = _recount(db, posting.id)
        kept += 1
    _touch_watermark(db, candidate.org_id, candidate.id, now)
    db.flush()
    return {"matched": kept}


def _upsert_match(
    db: Session, posting: JobPosting, candidate_id: str, pair: PairScore, *, now
) -> None:
    settings = get_settings()
    floor = settings.job_match_min_score
    row = db.scalar(
        select(JobMatch).where(
            JobMatch.job_posting_id == posting.id,
            JobMatch.candidate_id == candidate_id,
        )
    )
    above = pair.score >= floor
    if row is None:
        if not above:
            return
        db.add(
            JobMatch(
                org_id=posting.org_id,
                job_posting_id=posting.id,
                candidate_id=candidate_id,
                score=pair.score,
                heuristic_score=pair.heuristic_score,
                vector_score=pair.vector_score,
                criteria=pair.criteria,
                status="new",
                spec_version=posting.spec_version or "",
                computed_at=now,
            )
        )
        return
    if not above and row.status == "new":
        db.delete(row)
        return
    # Dismissed / shortlisted stay in that state; scores still refresh so the
    # ranked lists and rematch evidence stay current.
    row.score = pair.score
    row.heuristic_score = pair.heuristic_score
    row.vector_score = pair.vector_score
    row.criteria = pair.criteria
    row.spec_version = posting.spec_version or ""
    row.computed_at = now


def _drop_match(db: Session, posting_id: str, candidate_id: str) -> None:
    """Withdraw a match the heuristics no longer stand behind.

    Only ``new`` rows are deleted. A shortlist or a dismissal is a human
    decision with a reason attached, and the incumbency check that calls this
    reads mutable enrichment data -- a refreshed ``current_company``, or a
    corrected company on the posting, would otherwise silently erase that
    decision and let the pair come back as new on the next sweep. Same rule as
    ``_score_pair`` above.
    """
    row = db.scalar(
        select(JobMatch).where(
            JobMatch.job_posting_id == posting_id,
            JobMatch.candidate_id == candidate_id,
        )
    )
    if row is not None and row.status == "new":
        db.delete(row)


def _recount(db: Session, posting_id: str) -> int:
    settings = get_settings()
    count = db.scalar(
        select(func.count())
        .select_from(JobMatch)
        .where(
            JobMatch.job_posting_id == posting_id,
            JobMatch.status != "dismissed",
            JobMatch.score >= settings.job_match_min_score,
        )
    )
    return int(count or 0)


def _touch_watermark(db: Session, org_id: str, candidate_id: str, now) -> None:
    row = db.scalar(
        select(JobMatchWatermark).where(
            JobMatchWatermark.org_id == org_id,
            JobMatchWatermark.candidate_id == candidate_id,
        )
    )
    if row is None:
        db.add(JobMatchWatermark(org_id=org_id, candidate_id=candidate_id, computed_at=now))
        return
    row.computed_at = now


def _suppression_set(db: Session, org_id: str) -> set[str]:
    rows = db.scalars(select(Suppression.email).where(Suppression.org_id == org_id)).all()
    return {normalize_email(e) for e in rows if e}


def _is_suppressed_candidate(candidate: Candidate, suppressed: set[str]) -> bool:
    if not suppressed:
        return False
    return any(normalize_email(str(email)) in suppressed for email in candidate.emails or [])


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def enqueue_match_for_posting(db: Session, posting: JobPosting, *, now=None) -> None:
    now = now or utcnow()
    bucket = _refresh_bucket(now)
    enqueue(
        db,
        org_id=posting.org_id,
        kind=KIND_JOBS_MATCH_ONE,
        idempotency_key=(
            f"{KIND_JOBS_MATCH_ONE}:posting:{posting.id}:{posting.spec_version}:{bucket}"
        ),
        payload={"org_id": posting.org_id, "job_posting_id": posting.id},
        run_at=now,
    )


def enqueue_match_for_candidate(db: Session, candidate: Candidate, *, now=None) -> None:
    now = now or utcnow()
    stamp = int((candidate.updated_at or now).timestamp())
    enqueue(
        db,
        org_id=candidate.org_id,
        kind=KIND_JOBS_MATCH_ONE,
        idempotency_key=f"{KIND_JOBS_MATCH_ONE}:candidate:{candidate.id}:{stamp}",
        payload={"org_id": candidate.org_id, "candidate_id": candidate.id},
        run_at=now,
    )


def dispatch_match_tick(db: Session, org_id: str, *, now=None) -> dict:
    now = now or utcnow()
    settings = get_settings()
    refresh_after = now - timedelta(hours=max(settings.job_match_refresh_hours, 1))
    queued = 0
    postings = db.scalars(
        select(JobPosting).where(JobPosting.org_id == org_id, JobPosting.status == "open")
    ).all()
    for posting in postings:
        stale_spec = (posting.matched_spec_version or "") != (posting.spec_version or "")
        never = posting.matched_at is None
        matched_at = _aware(posting.matched_at)
        aged = matched_at is not None and matched_at < refresh_after
        if stale_spec or never or aged:
            enqueue_match_for_posting(db, posting, now=now)
            queued += 1

    watermarks = {
        row.candidate_id: row.computed_at
        for row in db.scalars(
            select(JobMatchWatermark).where(JobMatchWatermark.org_id == org_id)
        ).all()
    }
    candidates = db.scalars(select(Candidate).where(Candidate.org_id == org_id)).all()
    for candidate in candidates:
        mark = _aware(watermarks.get(candidate.id))
        updated = _aware(candidate.updated_at)
        if mark is None or (updated is not None and updated > mark):
            enqueue_match_for_candidate(db, candidate, now=now)
            queued += 1
    return {"queued": queued}


def handle_match_one(db: Session, job: Job, now) -> dict:
    payload = job.payload or {}
    posting_id = payload.get("job_posting_id")
    candidate_id = payload.get("candidate_id")
    if posting_id:
        posting = db.get(JobPosting, posting_id)
        if posting is None:
            return {"skipped": "missing_posting"}
        return match_posting(db, posting, now=now)
    if candidate_id:
        candidate = db.get(Candidate, candidate_id)
        if candidate is None:
            return {"skipped": "missing_candidate"}
        return match_candidate(db, candidate, now=now)
    return {"skipped": "empty_payload"}


def _refresh_bucket(now) -> int:
    settings = get_settings()
    hours = max(settings.job_match_refresh_hours, 1)
    return int(now.timestamp()) // (hours * 3600)
