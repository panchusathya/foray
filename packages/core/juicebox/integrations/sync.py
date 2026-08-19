"""Inbox sync: detect candidate replies in a connected Outlook mailbox.

The worker delta-polls each active mailbox (no public URL on the deploy VM, so
Graph change-notification webhooks are out; delta queries need nothing inbound).
The first sync stores a baseline delta link without reading history — old mail
is not outreach replies. Every later sync walks only what changed.

Matching an inbound email back to the outreach that provoked it:
1. by Graph conversationId against the outbound Message rows we sent, then
2. by sender address (``find_message_for_reply``) as the fallback.

Matched replies flow into ``record_reply`` — intent classification, stopping
the enrollment, pipeline stage, and the do-not-contact list all live there.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MailboxConnection, Message, ReplyDraft, utcnow
from ..outreach.sequencer import find_message_for_reply, record_reply
from .matching import build_address_index, record_synced_message, resolve_candidate
from .microsoft import GraphClient, MicrosoftError

logger = logging.getLogger(__name__)


@dataclass
class SyncSummary:
    initialized: bool = False
    seen: int = 0
    matched: int = 0
    skipped: int = 0
    intents: list[str] = field(default_factory=list)
    drafts_sent: int = 0
    drafts_discarded: int = 0
    # Thread sync (only moves when the mailbox opted in):
    sent_ingested: int = 0
    inbound_ingested: int = 0
    unmatched_discarded: int = 0


def _from_address(payload: dict) -> str:
    return (
        (payload.get("from") or {}).get("emailAddress", {}).get("address", "") or ""
    ).lower()


def _body_text(payload: dict) -> str:
    """Best plain-text body. The Prefer header asks Graph for text, but when a
    message arrives HTML anyway, the full content beats a 255-char preview."""
    body = payload.get("body") or {}
    if body.get("content"):
        return body["content"]
    return payload.get("bodyPreview") or ""


def _recipient_addresses(payload: dict) -> list[str]:
    addresses = []
    for key in ("toRecipients", "ccRecipients"):
        for entry in payload.get(key) or []:
            address = ((entry or {}).get("emailAddress") or {}).get("address", "")
            if address:
                addresses.append(address.lower())
    return addresses


def _already_recorded(db: Session, *, org_id: str, graph_message_id: str) -> bool:
    """Any direction: a sequence send stores the same ImmutableId that the
    Sent Items delta later returns, which is what stops double-storing it."""
    if not graph_message_id:
        return False
    return (
        db.scalar(
            select(Message.id).where(
                Message.org_id == org_id,
                Message.graph_message_id == graph_message_id,
            )
        )
        is not None
    )


def _match_outbound(
    db: Session, *, org_id: str, conversation_id: str, sender: str
) -> Message | None:
    if conversation_id:
        message = db.scalar(
            select(Message)
            .where(
                Message.org_id == org_id,
                Message.direction == "outbound",
                Message.conversation_id == conversation_id,
            )
            .order_by(Message.created_at.desc())
        )
        if message is not None:
            return message
    return find_message_for_reply(
        db, org_id=org_id, candidate_email=sender, provider_message_id=None
    )


def sync_mailbox(
    db: Session,
    connection: MailboxConnection,
    *,
    client: GraphClient | None = None,
    now: datetime | None = None,
) -> SyncSummary:
    """One delta pass over the mailbox's inbox. Raises MicrosoftError upward —
    the job queue owns retries and backoff."""
    now = now or utcnow()
    client = client or GraphClient(db, connection)
    summary = SyncSummary()

    if not connection.inbox_delta_link:
        connection.inbox_delta_link = client.inbox_delta_baseline()
        connection.sent_delta_link = client.sent_delta_baseline()
        connection.last_synced_at = now
        db.flush()
        summary.initialized = True
        return summary

    address_index = (
        build_address_index(db, org_id=connection.org_id)
        if connection.thread_sync_enabled
        else {}
    )

    messages, delta_link = client.inbox_delta(connection.inbox_delta_link)
    messages.sort(key=lambda m: m.get("receivedDateTime") or "")
    for payload in messages:
        summary.seen += 1
        sender = _from_address(payload)
        if (
            payload.get("isDraft")
            or not sender
            or sender == connection.address
            or payload.get("@removed") is not None
        ):
            summary.skipped += 1
            continue
        if _already_recorded(db, org_id=connection.org_id, graph_message_id=payload.get("id", "")):
            summary.skipped += 1
            continue
        outbound = _match_outbound(
            db,
            org_id=connection.org_id,
            conversation_id=payload.get("conversationId", ""),
            sender=sender,
        )
        if outbound is None:
            # Not a reply to anything we sent. With thread sync on, mail from a
            # known candidate still belongs in their timeline; strangers are
            # discarded without storing anything. Deliberately no
            # classify_reply / enrollment stop / pipeline move — there is no
            # sequence behind this email.
            candidate_id = resolve_candidate(address_index, [sender])
            if candidate_id is not None:
                record_synced_message(
                    db,
                    org_id=connection.org_id,
                    candidate_id=candidate_id,
                    mailbox_id=connection.id,
                    direction="inbound",
                    from_address=sender,
                    to_address=connection.address,
                    payload=payload,
                    now=now,
                )
                summary.inbound_ingested += 1
            else:
                summary.unmatched_discarded += 1
            summary.skipped += 1
            continue
        verdict = record_reply(
            db,
            message=outbound,
            subject=payload.get("subject", "") or "",
            body=_body_text(payload),
            now=now,
            mailbox_id=connection.id,
            graph_message_id=payload.get("id", ""),
            internet_message_id=payload.get("internetMessageId", ""),
            conversation_id=payload.get("conversationId", ""),
        )
        summary.matched += 1
        summary.intents.append(verdict.intent)
        _enqueue_reply_draft(db, connection=connection, graph_message_id=payload.get("id", ""))

    connection.inbox_delta_link = delta_link
    _sync_sent_items(db, connection, client, summary, now, address_index)
    connection.last_synced_at = now
    db.flush()
    return summary


# Everything below the first of these markers is quoted history, not the reply.
_QUOTE_MARKERS = re.compile(
    r"(?:\r?\n)+(?:"
    r"-----\s*Original Message\s*-----"
    r"|_{10,}"
    r"|From:\s.+"
    r"|On .{0,120}wrote:"
    r")",
    re.IGNORECASE,
)


def strip_quoted_history(text: str) -> str:
    """The user's own words from a sent reply, without the quoted thread."""
    return _QUOTE_MARKERS.split(text or "", maxsplit=1)[0].strip()


def _sync_sent_items(
    db: Session,
    connection: MailboxConnection,
    client: GraphClient,
    summary: SyncSummary,
    now: datetime,
    address_index: dict[str, str] | None = None,
) -> None:
    """Learn what happened to our reply drafts by watching Sent Items.

    A sent message with a draft's own id means the user sent (a possibly
    edited version of) our draft. A different message on the same conversation
    means they discarded ours and wrote their own — the strongest style
    signal of all. Either way the final text lands on the ReplyDraft row.
    """
    if not connection.sent_delta_link:
        connection.sent_delta_link = client.sent_delta_baseline()
        return
    messages, delta_link = client.sent_delta(connection.sent_delta_link)
    for payload in messages:
        if payload.get("@removed") is not None or payload.get("isDraft"):
            continue
        _ingest_sent_message(db, connection, payload, summary, now, address_index or {})
        sent_text = strip_quoted_history(_body_text(payload))
        draft = db.scalar(
            select(ReplyDraft).where(
                ReplyDraft.mailbox_id == connection.id,
                ReplyDraft.graph_draft_id == payload.get("id", ""),
            )
        )
        if draft is not None:
            if draft.status == "created":
                draft.status = "sent"
                draft.final_sent_text = sent_text
                draft.sent_at = now
                summary.drafts_sent += 1
            continue
        conversation_id = payload.get("conversationId", "")
        if not conversation_id:
            continue
        abandoned = db.scalar(
            select(ReplyDraft)
            .join(Message, Message.id == ReplyDraft.inbound_message_id)
            .where(
                ReplyDraft.mailbox_id == connection.id,
                ReplyDraft.status == "created",
                Message.conversation_id == conversation_id,
            )
        )
        if abandoned is not None and sent_text:
            abandoned.status = "discarded"
            abandoned.final_sent_text = sent_text
            abandoned.sent_at = now
            summary.drafts_discarded += 1
    connection.sent_delta_link = delta_link


def _ingest_sent_message(
    db: Session,
    connection: MailboxConnection,
    payload: dict,
    summary: SyncSummary,
    now: datetime,
    address_index: dict[str, str],
) -> None:
    """A manually sent email to a known candidate becomes a CRM row.

    Sequence sends are recognised by their ImmutableId (stored at send time)
    and skipped; mail to strangers is skipped without storing anything.
    """
    if not connection.thread_sync_enabled:
        return
    if _already_recorded(db, org_id=connection.org_id, graph_message_id=payload.get("id", "")):
        return
    recipients = _recipient_addresses(payload)
    candidate_id = resolve_candidate(address_index, recipients)
    if candidate_id is None:
        summary.unmatched_discarded += 1
        return
    to_address = next(
        (addr for addr in recipients if address_index.get(addr) == candidate_id), ""
    )
    record_synced_message(
        db,
        org_id=connection.org_id,
        candidate_id=candidate_id,
        mailbox_id=connection.id,
        direction="outbound",
        from_address=connection.address,
        to_address=to_address or (recipients[0] if recipients else ""),
        payload=payload,
        now=now,
    )
    summary.sent_ingested += 1


def _enqueue_reply_draft(
    db: Session, *, connection: MailboxConnection, graph_message_id: str
) -> None:
    """Queue drafting for one recorded reply. Opt-outs and automation get none."""
    from ..jobs import KIND_REPLY_DRAFT, enqueue
    from .reply_drafting import NO_DRAFT_INTENTS

    inbound = db.scalar(
        select(Message).where(
            Message.org_id == connection.org_id,
            Message.direction == "inbound",
            Message.graph_message_id == graph_message_id,
        )
    )
    if inbound is None or inbound.reply_intent in NO_DRAFT_INTENTS:
        return
    enqueue(
        db,
        org_id=connection.org_id,
        kind=KIND_REPLY_DRAFT,
        idempotency_key=f"{KIND_REPLY_DRAFT}:{inbound.id}",
        payload={
            "org_id": connection.org_id,
            "inbound_message_id": inbound.id,
            "mailbox_id": connection.id,
        },
    )


def sync_org_mailboxes(
    db: Session, *, org_id: str, now: datetime | None = None
) -> dict[str, SyncSummary]:
    """Sync every active mailbox in the org. One broken mailbox must not stop
    the others — its error lands on the connection row and the loop moves on."""
    now = now or utcnow()
    results: dict[str, SyncSummary] = {}
    connections = db.scalars(
        select(MailboxConnection).where(
            MailboxConnection.org_id == org_id,
            MailboxConnection.provider == "microsoft",
            MailboxConnection.status == "active",
        )
    ).all()
    for connection in connections:
        try:
            results[connection.address] = sync_mailbox(db, connection, now=now)
        except MicrosoftError as exc:
            logger.warning("mailbox sync failed for %s: %s", connection.address, exc)
            connection.error = str(exc)
            db.flush()
    return results
