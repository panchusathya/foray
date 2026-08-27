"""Writing a thread's record where matching can read it.

The merge itself moved to ``intake.records`` once the voice path needed it too;
``merge_record`` and ``record_from_preference`` are re-exported here so the
callers that grew up around this module keep working. What stays is the part
that is about a *thread*: which provenance column gets set, and what happens to
somebody's confirmation when the candidate's own words disagree with it.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..intake.records import EXTENSION_FIELDS, merge_record, record_from_preference
from ..intake.schema import SCHEMA_VERSION
from ..models import Candidate, CandidatePreference, IntakeThread, record_event, utcnow

logger = logging.getLogger(__name__)

# Re-exported: these were defined here before voice needed them.
__all__ = ["merge_record", "persist_preference", "record_from_preference"]


def persist_preference(
    db: Session, *, thread: IntakeThread, candidate_id: str
) -> CandidatePreference:
    """Write the thread's record onto the candidate's preference row.

    Upsert on ``candidate_id``: a person has one set of preferences, however
    many conversations produced them. The provenance columns record *which
    conversation last wrote this row*, so a thread write sets
    ``source_thread_id`` and clears ``source_call_id`` -- keeping exactly one
    of the pair set, while the merged content keeps everything the call found.

    Unlike ``intake.service.upsert_preference``, a confirmed row is written
    rather than refused: the candidate's own words outrank a recruiter's
    transcription of them. But the confirmation does not survive it. Leaving
    ``confirmed_at`` set over content the confirmer never saw is the one
    outcome worse than either overwriting or refusing, because every reader
    downstream trusts that flag.
    """
    pref = db.scalar(
        select(CandidatePreference).where(CandidatePreference.candidate_id == candidate_id)
    )
    created = pref is None
    previous = record_from_preference(pref) if pref is not None else {}
    if pref is None:
        pref = CandidatePreference(org_id=thread.org_id, candidate_id=candidate_id)
        db.add(pref)
        merged = merge_record(None, thread.record)
    else:
        merged = merge_record(previous, thread.record)

    invalidated = pref.confirmed_at is not None
    pref.source_thread_id = thread.id
    pref.source_call_id = None
    pref.spec = merged["spec"]
    pref.schema_version = merged.get("schema_version") or SCHEMA_VERSION
    for name in EXTENSION_FIELDS:
        value = merged[name]
        if name == "work_authorization":
            value = (value or "")[:120]
        setattr(pref, name, value)
    pref.declined = merged["declined"]
    pref.low_confidence = list(thread.low_confidence or [])
    pref.fields_missing = list(thread.fields_missing or [])
    pref.review_needed = bool(thread.review_needed)
    pref.review_reason = (thread.review_reason or "")[:400]
    if invalidated:
        # The row no longer says what the confirmer confirmed, so it must stop
        # claiming their sign-off. Back into the review queue, not silently
        # trusted.
        pref.confirmed_at = None
        pref.confirmed_by = ""
        pref.review_needed = True
        pref.review_reason = _join_reasons(
            pref.review_reason, f"thread {thread.id} changed a confirmed record"
        )
    pref.captured_at = utcnow()
    db.flush()
    from ..intake.facts import sync_preference_facts

    sync_preference_facts(
        db,
        preference=pref,
        previous=previous,
        current=merged,
        source_thread_id=thread.id,
        direct_candidate_statement=True,
        low_confidence=list(thread.low_confidence or []),
    )

    record_event(
        db,
        org_id=thread.org_id,
        type="intake.preference.captured" if created else "intake.preference.updated",
        subject_type="candidate",
        subject_id=candidate_id,
        payload={
            "thread_id": thread.id,
            "channel": thread.channel,
            "required_complete": bool(thread.required_complete),
            "review_needed": bool(thread.review_needed),
            "confirmation_cleared": invalidated,
        },
    )
    _enqueue_preference_match(db, thread, preference=pref, candidate_id=candidate_id)
    return pref


def _join_reasons(existing: str, addition: str) -> str:
    parts = [p for p in [(existing or "").strip(), addition] if p]
    return "; ".join(parts)[:400]


def _enqueue_preference_match(
    db: Session,
    thread: IntakeThread,
    *,
    preference: CandidatePreference,
    candidate_id: str,
) -> None:
    """Rescore against the jobs book, exactly as the voice path does.

    Without this a chat intake writes a preference nothing ever scores, so the
    match term fires for callers and silently not for texters. Imported inside
    the function because ``jobmatch.engine`` reaches back into ``intake``, and
    the stamp is ``preference.updated_at`` -- writing this row does not touch
    ``Candidate.updated_at``, which is what the candidate clock would key on.
    """
    candidate = db.get(Candidate, candidate_id)
    if candidate is None:
        return
    from ..jobmatch.discover_candidate import enqueue_discovery_for_candidate
    from ..jobmatch.engine import enqueue_match_for_candidate

    enqueue_match_for_candidate(db, candidate, preference=preference)
    # And go find postings that do not exist yet. Scoring against the book we
    # happen to hold is only half the job: the first live thread finished
    # onboarding with a complete spec and there was simply nothing in the
    # database for a two-year engineer in San Francisco. Discovery re-queues
    # its own match once the new postings are parsed.
    enqueue_discovery_for_candidate(db, candidate=candidate, preference=preference)
