"""Do-not-contact, on a channel where ignoring it is illegal rather than rude.

Wired into the existing ``Suppression`` list rather than a second one. A
candidate who told a recruiter to leave them alone has told *us* to leave them
alone, and a list that only outreach consults is a list that intake walks
straight past.

Two lookups, because there are two ways to be on it:

* **By candidate.** ``Suppression`` is keyed on an email address, so a thread
  bound to a candidate is checked against every address that candidate owns.
* **By address.** A stranger who texts STOP has no candidate row and therefore
  no address to suppress. Their opt-out is remembered as a *stopped thread*,
  and a later message from the same number finds it -- which is what
  ``ix_intake_threads_channel_from`` is for. Without this a STOP would bind
  only to the conversation it arrived in, and the next inbound text would open
  a fresh thread and start asking questions again.

The address path is a stopgap that closes before SMS ships, not a design: the
right fix is a phone column on ``Suppression``, which is a change to a table
the voice branch also writes to and so is deliberately not made here.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Candidate, CandidateEmail, IntakeThread
from ..outreach.suppressions import is_suppressed, suppress
from .threads import STATUS_STOPPED, close_thread, stopped_thread_exists

logger = logging.getLogger(__name__)

# The carrier-standard opt-out keywords. Matched against the whole trimmed
# message, not searched for inside it: "stop me if you've heard this one" is
# not an opt-out, and treating it as one loses a candidate silently.
STOP_WORDS = frozenset({"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke"})

# The em and en dashes are built with chr() rather than typed: a literal one
# is indistinguishable from a hyphen in most editors, and stripping the wrong
# character silently stops "STOP-" from matching.
_PUNCTUATION = ".,!?;:'\"()[]{}-" + chr(0x2014) + chr(0x2013)


def looks_like_stop(body: str) -> bool:
    return (body or "").strip().strip(_PUNCTUATION).strip().lower() in STOP_WORDS


def candidate_emails(db: Session, *, org_id: str, candidate_id: str) -> list[str]:
    """Every address we believe belongs to this candidate.

    Reads the ``candidate_emails`` projection and the JSON column both: the
    projection is a strict subset (a malformed address gets no row), and a
    suppression check that missed one would contact somebody who opted out.
    """
    rows = list(
        db.scalars(
            select(CandidateEmail.email).where(
                CandidateEmail.org_id == org_id, CandidateEmail.candidate_id == candidate_id
            )
        ).all()
    )
    candidate = db.get(Candidate, candidate_id)
    if candidate is not None:
        rows.extend(e for e in (candidate.emails or []) if isinstance(e, str))
    return list(dict.fromkeys(e.strip().lower() for e in rows if e and e.strip()))


def candidate_is_suppressed(db: Session, *, org_id: str, candidate_id: str) -> bool:
    return any(
        is_suppressed(db, org_id=org_id, email=email)
        for email in candidate_emails(db, org_id=org_id, candidate_id=candidate_id)
    )


def address_has_opted_out(db: Session, *, channel: str, from_address: str) -> bool:
    """Has this sender opted out on this channel, in any conversation?"""
    return stopped_thread_exists(db, channel=channel, from_address=from_address)


def is_blocked(db: Session, *, thread: IntakeThread) -> bool:
    """Must this thread stay silent?

    Checked before composing, not after: rendering a question for somebody on
    the do-not-contact list and then dropping it still costs a model call, and
    still leaves a half-written turn if anything downstream throws.
    """
    if thread.status == STATUS_STOPPED:
        return True
    if thread.contact_email and is_suppressed(
        db, org_id=thread.org_id, email=thread.contact_email
    ):
        return True
    if thread.candidate_id and candidate_is_suppressed(
        db, org_id=thread.org_id, candidate_id=thread.candidate_id
    ):
        return True
    return address_has_opted_out(db, channel=thread.channel, from_address=thread.from_address)


def handle_stop(db: Session, *, thread: IntakeThread) -> None:
    """Record an opt-out everywhere it needs to be visible.

    Immediate and unconditional: no confirmation message, no "are you sure".
    The thread closes, and every address we can attribute to the person goes on
    the suppression list so no other part of the stack contacts them either.
    """
    close_thread(db, thread=thread, status=STATUS_STOPPED, reason="opted out")

    addresses: list[str] = []
    if thread.contact_email:
        addresses.append(thread.contact_email)
    if thread.candidate_id:
        addresses.extend(
            candidate_emails(db, org_id=thread.org_id, candidate_id=thread.candidate_id)
        )
    for email in dict.fromkeys(addresses):
        suppress(
            db,
            org_id=thread.org_id,
            email=email,
            reason="unsubscribe",
            source="intake_thread",
            source_message_id=thread.id,
        )
    if not addresses:
        # Nothing to put on the list. The stopped thread is the only record,
        # which is why address_has_opted_out exists.
        logger.info("intake thread %s opted out with no address to suppress", thread.id)
