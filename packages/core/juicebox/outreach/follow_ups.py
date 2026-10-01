"""The follow-up reminder behind a "come back to me after <date>" reply.

A candidate who writes "In Japan until 9/10 please f/u after" has ended the
automated sequence -- a person answered -- and asked for a person to write
next. The sequencer stores the date on the enrollment; this module is what
happens the day after: one email to the mailbox the outreach went from,
saying who asked, what they wrote, and where to find them. No draft is
written into anybody's mailbox.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Candidate, Enrollment, MailboxConnection, Message, record_event, utcnow
from .sender import OutboundEmail, sender_for_connection
from .templates import to_html

logger = logging.getLogger(__name__)

RESULT_SENT = "sent"
RESULT_ALREADY_SENT = "already_sent"
RESULT_SUPERSEDED = "superseded"
RESULT_NO_MAILBOX = "no_mailbox"


def _mailbox_for(db: Session, enrollment: Enrollment, inbound: Message) -> MailboxConnection | None:
    """The mailbox the outreach went from, else the one the reply landed in."""
    for mailbox_id in (enrollment.mailbox_id, inbound.mailbox_id):
        if not mailbox_id:
            continue
        connection = db.get(MailboxConnection, mailbox_id)
        if connection is not None and connection.status == "active":
            return connection
    return None


def _long_date(value: date) -> str:
    # Built by hand: %-d is glibc-only and %#d is Windows.
    return f"{value.strftime('%A')} {value.day} {value.strftime('%B')}"


def reminder_email(
    *,
    connection: MailboxConnection,
    enrollment: Enrollment,
    inbound: Message,
    candidate: Candidate | None,
    follow_up_after: date,
) -> OutboundEmail:
    name = (candidate.full_name if candidate else "") or inbound.from_address or "A candidate"
    when = _long_date(follow_up_after)
    subject = f"Follow up: {name} asked to hear from you after {when}"

    reply = (inbound.reply_text or inbound.body or "").strip()
    if len(reply) > 600:
        reply = reply[:600] + "…"
    wrote_on = inbound.sent_at.strftime("%d %B") if inbound.sent_at else "recently"

    lines = [f"{name} asked you to follow up after {when}.", ""]
    if candidate is not None:
        role = " · ".join(
            part for part in (candidate.current_title, candidate.current_company) if part
        )
        if role:
            lines += [role, ""]
    lines += [
        f"Sequence: {enrollment.sequence.name}",
        f"They wrote on {wrote_on}:",
        "",
        reply or "(no text)",
        "",
        "The sequence stopped when they replied. This one is yours to write.",
    ]
    if candidate is not None:
        base_url = get_settings().base_url.rstrip("/")
        lines += ["", f"{base_url}/contacts/{candidate.id}"]
    text_body = "\n".join(lines)
    return OutboundEmail(
        to=connection.address,
        subject=subject,
        text_body=text_body,
        html_body=to_html(text_body),
        from_email=connection.address,
    )


def send_follow_up_reminder(
    db: Session,
    *,
    enrollment: Enrollment,
    inbound: Message,
    follow_up_after: date | None = None,
    now: datetime | None = None,
) -> str:
    """Mail the reminder once. Returns one of the RESULT_* strings.

    ``follow_up_after`` is the date the job was queued for; a later reply that
    named a different date supersedes it, and the enrollment's own date wins.
    A send failure raises so the job queue retries -- a reminder that quietly
    never arrives is the failure mode this exists to prevent.
    """
    now = now or utcnow()
    if enrollment.follow_up_reminder_sent_at is not None:
        return RESULT_ALREADY_SENT
    if enrollment.follow_up_after is None or (
        follow_up_after is not None and enrollment.follow_up_after != follow_up_after
    ):
        return RESULT_SUPERSEDED
    connection = _mailbox_for(db, enrollment, inbound)
    if connection is None:
        logger.warning(
            "follow-up reminder for enrollment %s has no active mailbox to send from",
            enrollment.id,
        )
        return RESULT_NO_MAILBOX

    candidate = db.get(Candidate, enrollment.candidate_id)
    email = reminder_email(
        connection=connection,
        enrollment=enrollment,
        inbound=inbound,
        candidate=candidate,
        follow_up_after=enrollment.follow_up_after,
    )
    result = sender_for_connection(db, connection).send(email)
    if not getattr(result, "ok", True):
        raise RuntimeError(
            f"follow-up reminder send failed: {getattr(result, 'error', '') or 'unknown error'}"
        )
    enrollment.follow_up_reminder_sent_at = now
    from .sequencer import note_mail_sent

    note_mail_sent(db, enrollment.sequence.org_id, now, connection=connection)
    record_event(
        db,
        org_id=enrollment.sequence.org_id,
        type="outreach.follow_up_reminded",
        subject_type="enrollment",
        subject_id=enrollment.id,
        payload={
            "candidate_id": enrollment.candidate_id,
            "follow_up_after": enrollment.follow_up_after.isoformat(),
            "mailbox": connection.address,
        },
    )
    db.flush()
    return RESULT_SENT
