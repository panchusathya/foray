"""Per-field provenance for reusable candidate preferences."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CandidatePreference, CandidatePreferenceFact, IntakeMessage, utcnow
from .records import EXTENSION_FIELDS
from .schema import is_filled


def _revision(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sync_preference_facts(
    db: Session,
    *,
    preference: CandidatePreference,
    previous: dict,
    current: dict,
    source_thread_id: str | None = None,
    source_call_id: str | None = None,
    direct_candidate_statement: bool = False,
    low_confidence: list | None = None,
) -> None:
    """Update metadata only for facts whose typed value actually changed."""
    latest_message_id = None
    if source_thread_id:
        latest_message_id = db.scalar(
            select(IntakeMessage.id)
            .where(IntakeMessage.thread_id == source_thread_id, IntakeMessage.direction == "inbound")
            .order_by(IntakeMessage.seq.desc())
            .limit(1)
        )
    uncertain = set(low_confidence or [])
    for field in EXTENSION_FIELDS:
        before, after = previous.get(field), current.get(field)
        declined = field in set(current.get("declined") or [])
        if before == after or (not is_filled(after) and not declined):
            continue
        row = db.scalar(
            select(CandidatePreferenceFact).where(
                CandidatePreferenceFact.candidate_preference_id == preference.id,
                CandidatePreferenceFact.field_key == field,
            )
        )
        if row is None:
            row = CandidatePreferenceFact(
                org_id=preference.org_id,
                candidate_id=preference.candidate_id,
                candidate_preference_id=preference.id,
                field_key=field,
            )
            db.add(row)
        row.source_thread_id = source_thread_id
        row.source_call_id = source_call_id
        row.source_message_id = latest_message_id
        row.value_revision = _revision(after)
        row.direct_candidate_statement = direct_candidate_statement and field not in uncertain
        row.declined = declined
        # A changed value must be reviewed independently; a prior row-level
        # confirmation cannot attest to a new assertion.
        row.confirmed_at = None
        row.captured_at = utcnow()
    db.flush()
