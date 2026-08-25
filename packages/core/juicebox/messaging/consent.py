"""Writing consent down where a dispute can find it.

CANDIDATE_AGENT_PLAN §2: choosing to mix sourced outbound with inbound texting
makes consent provenance load-bearing — the artifact *is* the defence. Every
place consent is obtained calls ``record_consent`` with the exact disclosure
that was shown and its version, because the wording changes over time and the
defence rests on what *this person* saw.

The suppression list remains what send paths consult. These rows are the audit
trail behind it, append-only: a revocation is recorded on the existing row's
``revoked_at`` (by the STOP path), never by deleting the grant.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ConsentRecord, record_event, utcnow

logger = logging.getLogger(__name__)

SCOPE_MESSAGING = "messaging"
SCOPE_APPLICATION_UPDATES = "application_updates"

SOURCE_WEB_WIDGET = "web_widget"
SOURCE_INBOUND_TEXT = "inbound_text"
SOURCE_VOICE_WRAP_UP = "voice_wrap_up"
SOURCE_PORTAL = "portal"

# Versioned disclosure texts. Bump the version whenever the wording changes so
# old rows keep describing what was actually shown at the time.
# v2 dropped the "and you can stop any time" clause from the greeting. The
# version is what makes that safe to do: old rows keep describing the wording
# those people actually saw, which is the entire point of storing the text
# rather than a reference to it.
DISCLOSURE_WEB_WIDGET = (
    "Person opened the intake chat widget and typed a message. Greeting shown: "
    '"Hi — I\'m the intake assistant for {org}. I\'m an AI, and I\'m here to '
    "take a few details so a recruiter can match you to the right roles. It "
    'takes a couple of minutes."'
)
DISCLOSURE_WEB_WIDGET_VERSION = "web-widget-v2"

# v2: the first reply no longer recites STOP/HELP. The keywords are still
# honoured -- that is a code path, not a sentence -- so this now says what is
# actually true of the message rather than what it used to contain.
DISCLOSURE_INBOUND_TEXT = (
    "Person texted our number first, unprompted. Prior express consent is the "
    "inbound message itself; the first reply identifies the org and discloses "
    "the AI. STOP/HELP are honoured per carrier keywords, and STOP is answered "
    "with a single acknowledgement, though neither is recited in the greeting."
)
DISCLOSURE_INBOUND_TEXT_VERSION = "inbound-text-v2"

# The voice wrap-up question, verbatim from INTAKE_COMPLIANCE.md §6 and the
# deployed agent definition. Consent is for application updates, not marketing.
# v2 dropped the opt-out sentence that followed the question. The *question* is
# the consent and is unchanged -- removing the recital does not weaken what was
# asked, and keeping the version honest is what lets a dispute about a v1 grant
# still be answered with the words that person heard.
DISCLOSURE_VOICE_SMS = (
    'Asked at call wrap-up, verbatim: "Can I text you updates at this number?" '
    "Only an explicit, unambiguous yes is recorded as consent; the agent does "
    "not re-ask or reframe. STOP is honoured on any later message."
)
DISCLOSURE_VOICE_SMS_VERSION = "voice-wrapup-v2"


def record_consent(
    db: Session,
    *,
    org_id: str,
    channel: str,
    scope: str,
    source: str,
    disclosure_text: str,
    disclosure_version: str,
    candidate_id: str | None = None,
    thread_id: str | None = None,
    call_id: str | None = None,
    verbatim: str = "",
    actor_id: str | None = None,
) -> ConsentRecord:
    """Append one consent grant. Idempotent per (thread|call, scope).

    A webhook retry or a re-run wrap-up analysis must not stack duplicate
    grants — the second write returns the row the first one made.
    """
    existing = None
    if thread_id:
        existing = db.scalar(
            select(ConsentRecord).where(
                ConsentRecord.thread_id == thread_id, ConsentRecord.scope == scope
            )
        )
    elif call_id:
        existing = db.scalar(
            select(ConsentRecord).where(
                ConsentRecord.call_id == call_id, ConsentRecord.scope == scope
            )
        )
    if existing is not None:
        return existing

    row = ConsentRecord(
        org_id=org_id,
        candidate_id=candidate_id,
        thread_id=thread_id,
        call_id=call_id,
        channel=channel,
        scope=scope,
        source=source,
        disclosure_text=disclosure_text,
        disclosure_version=disclosure_version,
        verbatim=(verbatim or "")[:400],
        granted=True,
        actor_id=actor_id,
    )
    db.add(row)
    db.flush()
    record_event(
        db,
        org_id=org_id,
        type="consent.recorded",
        subject_type="consent_record",
        subject_id=row.id,
        payload={"channel": channel, "scope": scope, "source": source},
    )
    return row


def revoke_consent(db: Session, *, thread_id: str) -> int:
    """Stamp every grant on a thread as revoked. Returns how many were.

    Called by the STOP path. The suppression list is what actually silences
    sends; this keeps the audit trail telling the same story.
    """
    rows = list(
        db.scalars(
            select(ConsentRecord).where(
                ConsentRecord.thread_id == thread_id,
                ConsentRecord.revoked_at.is_(None),
            )
        ).all()
    )
    now = utcnow()
    for row in rows:
        row.revoked_at = now
    if rows:
        db.flush()
    return len(rows)
