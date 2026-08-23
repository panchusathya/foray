"""Merging what a thread has learned, and writing it where matching can read it.

Two jobs, one theme: a thread is re-extracted on every turn, so the same field
gets answered many times over the life of a conversation and something has to
decide which answer stands.

**The merge is monotonic.** A field that has been answered never returns to
empty. Each turn re-runs ``extract_intake`` over the whole transcript, and a
later pass can legitimately come back quieter than an earlier one -- the model
hedges, or the heuristic path takes over because the key went away mid-thread.
Letting that clear a filled field would reopen the coverage gate and re-ask a
question the candidate already answered, which on a messaging channel is the
failure that gets a number blocked rather than merely disliked.

A *replacement* still wins: re-extraction reads the whole conversation, so
"actually, make that 260" arrives as a filled value and overwrites. It is only
the empty-over-filled direction that is refused.

``declined`` is a union, never a replacement, for the same reason it counts as
covered in ``intake.schema.coverage``: a refusal is an answer, and an answer
cannot be un-given by a later pass that failed to notice it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..intake.schema import SCHEMA_VERSION, SPEC_FIELDS, empty_record, is_filled
from ..models import CandidatePreference, IntakeThread, record_event, utcnow

logger = logging.getLogger(__name__)

# Record keys that map straight onto CandidatePreference columns of the same
# name. `spec` and `declined` are handled separately; everything else here is
# a one-to-one copy, and the test that walks empty_record() keeps it honest.
_EXTENSION_FIELDS: tuple[str, ...] = (
    "employment_status",
    "reason_for_looking",
    "timeline",
    "urgency_driver",
    "target_base",
    "target_total",
    "comp_currency",
    "comp_flexibility_notes",
    "equity_importance",
    "work_authorization",
    "willing_to_relocate",
    "active_processes",
    "offers_in_hand",
    "highlights",
    "confidential_items",
    "ok_to_share_with_clients",
)


def merge_record(current: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict:
    """Fold a fresh extraction into the record a thread has accumulated.

    Filled beats empty, newer beats older, and ``declined`` accumulates.
    """
    merged = empty_record()
    merged.update({k: v for k, v in (current or {}).items() if k not in ("spec", "declined")})
    merged["spec"] = dict(empty_record()["spec"])
    merged["spec"].update((current or {}).get("spec") or {})

    incoming = incoming or {}
    for key, value in incoming.items():
        if key in ("spec", "declined", "schema_version"):
            continue
        if is_filled(value):
            merged[key] = value

    for name in SPEC_FIELDS:
        value = (incoming.get("spec") or {}).get(name)
        if is_filled(value):
            merged["spec"][name] = value

    declined = list(dict.fromkeys([*(current or {}).get("declined", []), *incoming.get("declined", [])]))
    merged["declined"] = declined
    merged["schema_version"] = SCHEMA_VERSION
    return merged


def record_from_preference(pref: CandidatePreference) -> dict:
    """Read a stored preference row back into record shape.

    So a thread from someone who already called merges with what the call
    captured instead of replacing it. Losing half a phone conversation because
    the same person later sent a text would be a bad trade, and the merge rule
    that makes threads safe makes this safe too.
    """
    record = empty_record()
    record["spec"].update(pref.spec or {})
    for name in _EXTENSION_FIELDS:
        record[name] = getattr(pref, name)
    record["declined"] = list(pref.declined or [])
    return record


def persist_preference(
    db: Session, *, thread: IntakeThread, candidate_id: str
) -> CandidatePreference:
    """Write the thread's record onto the candidate's preference row.

    Upsert on ``candidate_id``: a person has one set of preferences, however
    many conversations produced them. The provenance columns record *which
    conversation last wrote this row*, so a thread write sets
    ``source_thread_id`` and clears ``source_call_id`` -- keeping exactly one
    of the pair set, while the merged content keeps everything the call found.
    """
    pref = db.scalar(
        select(CandidatePreference).where(CandidatePreference.candidate_id == candidate_id)
    )
    created = pref is None
    if pref is None:
        pref = CandidatePreference(org_id=thread.org_id, candidate_id=candidate_id)
        db.add(pref)
        merged = merge_record(None, thread.record)
    else:
        merged = merge_record(record_from_preference(pref), thread.record)

    pref.source_thread_id = thread.id
    pref.source_call_id = None
    pref.spec = merged["spec"]
    pref.schema_version = merged.get("schema_version") or SCHEMA_VERSION
    for name in _EXTENSION_FIELDS:
        setattr(pref, name, merged[name])
    pref.declined = merged["declined"]
    pref.low_confidence = list(thread.low_confidence or [])
    pref.fields_missing = list(thread.fields_missing or [])
    pref.review_needed = bool(thread.review_needed)
    pref.review_reason = (thread.review_reason or "")[:400]
    pref.captured_at = utcnow()
    db.flush()

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
        },
    )
    return pref
