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
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MailboxConnection, Message, utcnow
from ..outreach.sequencer import find_message_for_reply, record_reply
from .microsoft import GraphClient, MicrosoftError

logger = logging.getLogger(__name__)


@dataclass
class SyncSummary:
    initialized: bool = False
    seen: int = 0
    matched: int = 0
    skipped: int = 0
    intents: list[str] = field(default_factory=list)


def _from_address(payload: dict) -> str:
    return (
        (payload.get("from") or {}).get("emailAddress", {}).get("address", "") or ""
    ).lower()


def _body_text(payload: dict) -> str:
    body = payload.get("body") or {}
    if (body.get("contentType") or "").lower() == "text" and body.get("content"):
        return body["content"]
    return payload.get("bodyPreview") or body.get("content") or ""


def _already_recorded(db: Session, *, org_id: str, graph_message_id: str) -> bool:
    if not graph_message_id:
        return False
    return (
        db.scalar(
            select(Message.id).where(
                Message.org_id == org_id,
                Message.direction == "inbound",
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
        connection.last_synced_at = now
        db.flush()
        summary.initialized = True
        return summary

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

    connection.inbox_delta_link = delta_link
    connection.last_synced_at = now
    db.flush()
    return summary


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
