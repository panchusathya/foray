"""One intake record folded into another, and read back off a stored row.

Lives in ``intake`` rather than ``messaging`` because both channels need it and
``messaging`` already imports ``intake`` -- the other direction is a cycle. The
text channel was first to need a merge and grew it; the voice channel wrote its
record straight over the row, which is how a call could erase what a text
conversation had captured about the same person.

**The merge is monotonic.** A field that has been answered never returns to
empty. A thread re-runs ``extract_intake`` over the whole transcript on every
turn and a later pass can legitimately come back quieter than an earlier one --
the model hedges, or the heuristic path takes over because the API key went
away mid-thread. Letting that clear a filled field reopens the coverage gate
and re-asks a question the candidate already answered, which on a messaging
channel is the failure that gets a number blocked rather than merely disliked.

A *replacement* still wins: re-extraction reads the whole conversation, so
"actually, make that 260" arrives as a filled value and overwrites. It is only
the empty-over-filled direction that is refused.

``declined`` is a union, never a replacement, for the same reason it counts as
covered in ``intake.schema.coverage``: a refusal is an answer, and an answer
cannot be un-given by a later pass that failed to notice it.
"""

from __future__ import annotations

from typing import Any

from ..models import CandidatePreference
from .schema import SCHEMA_VERSION, SPEC_FIELDS, empty_record, is_filled

# Record keys that map straight onto CandidatePreference columns of the same
# name. `spec` and `declined` are handled separately; everything else here is
# a one-to-one copy, and the test that walks empty_record() keeps it honest.
EXTENSION_FIELDS: tuple[str, ...] = (
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
    for name in EXTENSION_FIELDS:
        record[name] = getattr(pref, name)
    record["declined"] = list(pref.declined or [])
    return record


__all__ = [
    "EXTENSION_FIELDS",
    "merge_record",
    "record_from_preference",
]
