"""Draft a reply to ordinary inbox mail and hold it for review.

The difference from ``reply_drafting`` is where the draft goes. That module
answers a candidate who replied to a sequence, and writes into the mailbox
immediately -- safe, because something the user sent started the thread. Here
the trigger is only that somebody emailed them, so nothing touches the mailbox
until a human has read the draft and approved it. Until then the row is the
whole artefact.

Generating offline is deliberate. The template path produces an honest
acknowledgement rather than a guess, so a deployment with no model still gets a
queue it can work through; the LLM path is better prose, not a different
feature.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from ..llm.client import get_llm
from ..llm.prompts import INBOX_DRAFT_SCHEMA, INBOX_DRAFT_SYSTEM, inbox_draft_prompt
from ..models import (
    Candidate,
    InboxDraft,
    MailboxConnection,
    new_id,
    record_event,
    utcnow,
)
from .providers import ProviderError, get_provider
from .reply_drafting import recent_style_examples

logger = logging.getLogger(__name__)

# Long enough to answer, short enough not to pay for a forwarded thread.
_BODY_BUDGET = 6000

_TEMPLATE = (
    "Thanks for the note{name_clause} — good to hear from you.\n\n"
    "Let me come back to you properly on this shortly."
)


def _first_name(candidate: Candidate | None, from_name: str) -> str:
    if candidate and candidate.first_name:
        return candidate.first_name
    source = (from_name or "").strip()
    return source.split()[0] if source else ""


def _template_body(candidate: Candidate | None, from_name: str) -> str:
    first = _first_name(candidate, from_name)
    return _TEMPLATE.format(name_clause=f", {first}" if first else "")


def _context(db, candidate: Candidate | None, from_address: str) -> str:
    """What we know about the sender, for the prompt. Empty is fine."""
    if candidate is None:
        return ""
    bits = [candidate.full_name or from_address]
    if candidate.current_title:
        bits.append(candidate.current_title)
    if candidate.current_company:
        bits.append(f"at {candidate.current_company}")
    line = " — ".join([bits[0], ", ".join(bits[1:])]) if len(bits) > 1 else bits[0]
    return f"{line}\nThis person is already a contact in the CRM."


def generate_body(
    db,
    *,
    draft: InboxDraft,
    candidate: Candidate | None,
    mailbox_id: str = "",
) -> tuple[str, str]:
    """Return (body, generated_by). An empty body means a human should write it."""
    llm = get_llm()
    if llm.available:
        try:
            result = llm.json(
                system=INBOX_DRAFT_SYSTEM,
                prompt=inbox_draft_prompt(
                    context=_context(db, candidate, draft.from_address),
                    from_line=(
                        f"{draft.from_name} <{draft.from_address}>"
                        if draft.from_name
                        else draft.from_address
                    ),
                    subject=draft.subject,
                    inbound_body=(draft.inbound_body or "")[:_BODY_BUDGET],
                    style_examples=(
                        recent_style_examples(db, mailbox_id=mailbox_id) if mailbox_id else ""
                    ),
                ),
                schema=INBOX_DRAFT_SCHEMA,
                effort="medium",
                max_tokens=2000,
            )
            data = result.data or {}
            # An explicit "a human should write this" is an answer, not a
            # failure, so it must not fall through to the template -- the
            # template would then paper over exactly the cases the model
            # flagged as needing care.
            if data.get("needs_a_human"):
                return "", "llm"
            body = str(data.get("body", "")).strip()
            if body:
                return body, "llm"
        except Exception as exc:  # noqa: BLE001 - fall back rather than skip the draft
            logger.warning("inbox drafting failed, using template: %s", exc)
    return _template_body(candidate, draft.from_name), "template"


def create_inbox_draft(
    db,
    *,
    connection: MailboxConnection,
    org_id: str,
    inbound_native_id: str,
    from_address: str,
    from_name: str = "",
    subject: str = "",
    inbound_body: str = "",
    thread_id: str = "",
    rfc_message_id: str = "",
    received_at: datetime | None = None,
    web_link: str = "",
    candidate_id: str | None = None,
    message_id: str | None = None,
    now: datetime | None = None,
) -> InboxDraft | None:
    """Record the message to be answered. Returns None when already queued.

    The body is deliberately not written here. This runs inside the mailbox
    sync loop, and a model call per message would turn a poll over a busy
    inbox into a long transaction that holds its rows the whole time. The row
    lands first, the wording follows in ``generate_for_draft``.

    The uniqueness check is done here as well as in the schema: the sync loop
    re-reads a folder on every poll, and letting the constraint raise would
    roll back the whole sync transaction over a message we simply already know
    about.
    """
    now = now or utcnow()
    existing = db.scalar(
        select(InboxDraft).where(
            InboxDraft.org_id == org_id,
            InboxDraft.inbound_native_id == inbound_native_id,
        )
    )
    if existing is not None:
        return None

    draft = InboxDraft(
        id=new_id("idr"),
        org_id=org_id,
        mailbox_id=connection.id,
        inbound_native_id=inbound_native_id,
        inbound_thread_id=thread_id,
        inbound_rfc_message_id=rfc_message_id,
        from_address=from_address,
        from_name=from_name,
        subject=subject,
        inbound_body=inbound_body or "",
        received_at=received_at,
        web_link=web_link,
        candidate_id=candidate_id,
        message_id=message_id,
        status="pending",
        # Empty until the worker has written one. The queue shows the message
        # either way, so a model outage costs the wording, not the row.
        generated_by="",
    )
    db.add(draft)
    db.flush()
    record_event(
        db,
        org_id=org_id,
        type="inbox_draft.created",
        subject_type="inbox_draft",
        subject_id=draft.id,
        payload={"mailbox_id": connection.id, "candidate_id": candidate_id},
    )
    return draft


def generate_for_draft(db, *, draft: InboxDraft, now: datetime | None = None) -> InboxDraft:
    """Write the wording onto a row the sync loop already recorded.

    Never overwrites an edit: once somebody has typed into the draft, a retry
    of the generation job must not replace their words with ours.
    """
    now = now or utcnow()
    if draft.generated_by and draft.body != draft.generated_body:
        return draft
    candidate = db.get(Candidate, draft.candidate_id) if draft.candidate_id else None
    body, generated_by = generate_body(
        db, draft=draft, candidate=candidate, mailbox_id=draft.mailbox_id
    )
    draft.body = body
    draft.generated_body = body
    draft.generated_by = generated_by
    db.flush()
    record_event(
        db,
        org_id=draft.org_id,
        type="inbox_draft.generated",
        subject_type="inbox_draft",
        subject_id=draft.id,
        payload={
            "generated_by": generated_by,
            # An empty body is the model saying this one deserves a person.
            "needs_a_human": not body,
        },
    )
    return draft


def place_in_mailbox(db, *, draft: InboxDraft, now: datetime | None = None) -> InboxDraft:
    """Push an approved draft onto the real thread in the user's mailbox.

    The one write this feature makes, and only ever from an explicit approval.
    A provider failure is recorded on the row and left ``pending`` so the user
    can try again -- marking it placed when nothing reached the mailbox would
    lose the reply silently.
    """
    from ..outreach.templates import to_html

    now = now or utcnow()
    connection = db.get(MailboxConnection, draft.mailbox_id)
    if connection is None or connection.status != "active":
        raise ProviderError("mailbox is not connected")

    provider = get_provider(connection.provider)
    try:
        result = provider.place_reply_draft(
            db,
            connection=connection,
            inbound=_InboundView(draft, connection),
            html_body=to_html(draft.body),
        )
    except ProviderError:
        raise
    except Exception as exc:
        # ``ProviderError`` is the contract for "the mailbox side failed", and
        # the most ordinary failure of all does not honour it: an expired or
        # re-keyed token blob comes out of the crypto layer as a bare
        # ValueError. Uncaught, the caller returns a 500 saying nothing, and
        # the reason never reaches ``draft.error`` -- so the user is told
        # "Internal Server Error" about a mailbox they simply need to
        # reconnect.
        raise ProviderError(str(exc) or exc.__class__.__name__) from exc
    draft.provider_draft_id = result.provider_draft_id
    draft.draft_web_link = result.web_link
    draft.status = "placed"
    draft.error = ""
    draft.reviewed_at = now
    db.flush()
    record_event(
        db,
        org_id=draft.org_id,
        type="inbox_draft.placed",
        subject_type="inbox_draft",
        subject_id=draft.id,
        payload={"mailbox_id": draft.mailbox_id},
    )
    return draft


class _InboundView:
    """The fields ``place_reply_draft`` reads off a ``Message``.

    The providers take the inbound message being replied to, and an InboxDraft
    is not one -- a stranger's email is never stored as a Message. Rather than
    widen the provider protocol for this one caller, the row is presented in
    the shape they already accept.

    Every attribute either provider touches has to be here, including the ones
    only one of them reads: Graph replies by ``provider_native_id`` alone, but
    Zoho has no create-reply call, so it builds the draft by hand and needs the
    address to answer, the subject to prefix and the body to quote underneath.
    ``body_html`` and ``sent_at`` are read with ``getattr`` defaults, and are
    supplied so the quoted block is not left saying "earlier".
    """

    __slots__ = (
        "body",
        "body_html",
        "conversation_id",
        "from_address",
        "internet_message_id",
        "provider_native_id",
        "sent_at",
        "subject",
        "to_address",
    )

    def __init__(self, draft: InboxDraft, connection: MailboxConnection) -> None:
        self.provider_native_id = draft.inbound_native_id
        self.conversation_id = draft.inbound_thread_id
        self.internet_message_id = draft.inbound_rfc_message_id
        self.subject = draft.subject
        self.body = draft.inbound_body
        # Nothing stores the inbound HTML, so the quoter escapes the plain text
        # instead -- which is what it does for a Message with no HTML either.
        self.body_html = ""
        self.from_address = draft.from_address
        self.to_address = connection.address
        self.sent_at = draft.received_at
