"""Inbox sync: detect candidate replies in any connected mailbox.

The worker polls each active mailbox (no public URL on the deploy VM, so Graph
change notifications are out, and Zoho has no webhooks at all). How a provider
tracks its position is its own business -- Graph walks a delta link, Zoho keeps
a timestamp high-water mark -- and both hand this module the same
``MailboxEvent`` stream. Nothing below this line knows a provider's field names.

The first pass over a folder only records where "now" is and reads no history:
old mail is not outreach replies. Every later pass walks what arrived since.

Matching an inbound email back to the outreach that provoked it, cheapest and
strictest first:

1. by thread id (Graph conversationId, Zoho threadId) against outbound rows,
2. by the reply's In-Reply-To against the RFC Message-ID we stored on send,
3. by sender address (``find_message_for_reply``) as the last resort.

(1) checks both conversation_id and provider_native_id, because the two
providers mean different things by "thread id": Graph mints a separate
conversationId, while Zoho reuses the root message's own id.

(2) exists because address matching cannot tell two sequences to the same
person apart, and because not every reply carries a thread id. Providers that
need an extra call to populate it only make that call when (1) came up empty.

Matched replies flow into ``record_reply`` -- intent classification, stopping
the enrollment, pipeline stage, and the do-not-contact list all live there.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import MailboxConnection, Message, ReplyDraft, utcnow
from ..outreach.sequencer import find_message_for_reply, record_reply
from .matching import build_address_index, record_synced_message, resolve_candidate
from .providers import FOLDER_INBOX, FOLDER_SENT, MailboxEvent, get_provider
from .replies import clean_inbound_body

logger = logging.getLogger(__name__)

# One sync pass reads at most this many pages per folder. Providers that page
# by offset (Zoho) would otherwise let one very stale mailbox monopolise the
# worker; the next tick picks up where this one stopped.
MAX_PAGES_PER_FOLDER = 10


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


def _already_recorded(db: Session, *, org_id: str, native_id: str) -> bool:
    """Any direction: a sequence send stores the same native id the Sent folder
    later returns, which is what stops double-storing it."""
    if not native_id:
        return False
    return (
        db.scalar(
            select(Message.id).where(
                Message.org_id == org_id,
                Message.provider_native_id == native_id,
            )
        )
        is not None
    )


def _match_outbound(db: Session, *, org_id: str, event: MailboxEvent) -> Message | None:
    if event.thread_id:
        # Two shapes of thread id, one query. Graph mints a conversationId that
        # looks nothing like a message id, and we store it in conversation_id.
        # Zoho reuses the ROOT message's own messageId as the thread id, which
        # for our sends lives in provider_native_id -- checking only
        # conversation_id meant no Zoho reply ever matched by thread, quietly
        # demoting every one of them to sender-address guesswork.
        message = db.scalar(
            select(Message)
            .where(
                Message.org_id == org_id,
                Message.direction == "outbound",
                or_(
                    Message.conversation_id == event.thread_id,
                    Message.provider_native_id == event.thread_id,
                ),
            )
            .order_by(Message.created_at.desc())
        )
        if message is not None:
            return message
    if event.in_reply_to:
        message = db.scalar(
            select(Message)
            .where(
                Message.org_id == org_id,
                Message.direction == "outbound",
                Message.internet_message_id == event.in_reply_to,
            )
            .order_by(Message.created_at.desc())
        )
        if message is not None:
            return message
    return find_message_for_reply(
        db, org_id=org_id, candidate_email=event.from_address, provider_message_id=None
    )


def _read_folder(
    db: Session,
    connection: MailboxConnection,
    provider,
    folder: str,
    *,
    cursor: str,
    handle,
) -> bool:
    """Walk one folder, handing each page to ``handle`` before its cursor lands.

    Committing the cursor only after the page is recorded means a crash
    mid-page replays rather than skips; the idempotent ingest makes the replay
    free. Returns True when this call only established a baseline.
    """
    for _ in range(MAX_PAGES_PER_FOLDER):
        page = provider.iter_folder(db, connection, folder, cursor=cursor)
        if page.initialized:
            provider.commit_cursor(db, connection, folder, page.next_cursor)
            return True
        events = sorted(
            page.events, key=lambda item: item.received_at or item.sent_at or utcnow()
        )
        for event in events:
            handle(event)
        provider.commit_cursor(db, connection, folder, page.next_cursor)
        if not page.has_more:
            return False
        cursor = page.next_cursor
    return False


def sync_mailbox(
    db: Session,
    connection: MailboxConnection,
    *,
    provider=None,
    now: datetime | None = None,
) -> SyncSummary:
    """One pass over a mailbox's inbox and sent folder. Raises ProviderError
    upward -- the job queue owns retries and backoff."""
    now = now or utcnow()
    provider = provider or get_provider(connection.provider)
    summary = SyncSummary()

    address_index = (
        build_address_index(db, org_id=connection.org_id)
        if connection.thread_sync_enabled
        else {}
    )

    def handle_inbound(event: MailboxEvent) -> None:
        _handle_inbound_event(
            db,
            connection,
            event,
            summary=summary,
            now=now,
            address_index=address_index,
        )

    baselined = _read_folder(
        db,
        connection,
        provider,
        FOLDER_INBOX,
        cursor=provider.read_cursor(connection, FOLDER_INBOX),
        handle=handle_inbound,
    )
    if baselined:
        # Baseline the sent cursor in the same pass so the next one reads both.
        _read_folder(
            db,
            connection,
            provider,
            FOLDER_SENT,
            cursor=provider.read_cursor(connection, FOLDER_SENT),
            handle=lambda event: None,
        )
        connection.last_synced_at = now
        db.flush()
        summary.initialized = True
        return summary

    def handle_sent(event: MailboxEvent) -> None:
        _handle_sent_event(
            db,
            connection,
            event,
            summary=summary,
            now=now,
            address_index=address_index,
        )

    _read_folder(
        db,
        connection,
        provider,
        FOLDER_SENT,
        cursor=provider.read_cursor(connection, FOLDER_SENT),
        handle=handle_sent,
    )

    connection.last_synced_at = now
    db.flush()
    if summary.seen or summary.matched or summary.unmatched_discarded:
        # A silent discard once cost two real candidate replies during deploy
        # churn and was undiagnosable afterwards — the consuming container's
        # logs were gone and the job row just said "succeeded". Every non-empty
        # pass now leaves a durable trace of what it did with each count.
        logger.info(
            "mailbox sync %s (%s): seen=%d matched=%d skipped=%d intents=%s "
            "sent_ingested=%d inbound_ingested=%d unmatched_discarded=%d",
            connection.address,
            connection.provider,
            summary.seen,
            summary.matched,
            summary.skipped,
            summary.intents,
            summary.sent_ingested,
            summary.inbound_ingested,
            summary.unmatched_discarded,
        )
    return summary


def _handle_inbound_event(
    db: Session,
    connection: MailboxConnection,
    event: MailboxEvent,
    *,
    summary: SyncSummary,
    now: datetime,
    address_index: dict[str, str],
) -> None:
    summary.seen += 1
    # Once, here, before anything reads the body. Everything downstream --
    # the timeline a recruiter reads, the intent classifier, the reply
    # drafter -- was being handed raw markup with our own outreach quoted
    # inside it. The classifier was the real casualty: it was deciding
    # intent partly from our pitch, attributed to the candidate.
    event = replace(event, body=clean_inbound_body(event.body, is_html=event.body_is_html))
    if (
        event.is_draft
        or event.removed
        or not event.from_address
        or event.from_address == connection.address
    ):
        summary.skipped += 1
        return
    if _already_recorded(db, org_id=connection.org_id, native_id=event.native_id):
        summary.skipped += 1
        return
    outbound = _match_outbound(db, org_id=connection.org_id, event=event)
    if outbound is None:
        # Not a reply to anything we sent. With thread sync on, mail from a
        # known candidate still belongs in their timeline; strangers are
        # discarded without storing anything. Deliberately no classify_reply /
        # enrollment stop / pipeline move — there is no sequence behind this.
        candidate_id = resolve_candidate(address_index, [event.from_address])
        if candidate_id is not None:
            record_synced_message(
                db,
                org_id=connection.org_id,
                candidate_id=candidate_id,
                mailbox_id=connection.id,
                direction="inbound",
                from_address=event.from_address,
                to_address=connection.address,
                event=event,
                now=now,
                source=connection.provider,
            )
            summary.inbound_ingested += 1
        else:
            summary.unmatched_discarded += 1
        summary.skipped += 1
        return
    verdict = record_reply(
        db,
        message=outbound,
        subject=event.subject,
        body=event.body,
        now=now,
        mailbox_id=connection.id,
        provider_native_id=event.native_id,
        internet_message_id=event.rfc_message_id,
        conversation_id=event.thread_id,
    )
    summary.matched += 1
    summary.intents.append(verdict.intent)
    _enqueue_reply_draft(db, connection=connection, native_id=event.native_id)


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


def _handle_sent_event(
    db: Session,
    connection: MailboxConnection,
    event: MailboxEvent,
    *,
    summary: SyncSummary,
    now: datetime,
    address_index: dict[str, str],
) -> None:
    """Learn what happened to our reply drafts by watching the Sent folder.

    A sent message carrying a draft's own id means the user sent (a possibly
    edited version of) our draft. A different message on the same thread means
    they discarded ours and wrote their own — the strongest style signal of
    all. Either way the final text lands on the ReplyDraft row.

    Zoho does not guarantee that a draft keeps its id once sent, so the
    draft-id match is tried first and the thread match catches the rest.
    """
    if event.removed or event.is_draft:
        return
    _ingest_sent_message(db, connection, event, summary, now, address_index)
    sent_text = strip_quoted_history(event.body)
    draft = db.scalar(
        select(ReplyDraft).where(
            ReplyDraft.mailbox_id == connection.id,
            ReplyDraft.provider_draft_id == event.native_id,
        )
    )
    if draft is not None:
        if draft.status == "created":
            draft.status = "sent"
            draft.final_sent_text = sent_text
            draft.sent_at = now
            summary.drafts_sent += 1
        return
    if not event.thread_id:
        return
    abandoned = db.scalar(
        select(ReplyDraft)
        .join(Message, Message.id == ReplyDraft.inbound_message_id)
        .where(
            ReplyDraft.mailbox_id == connection.id,
            ReplyDraft.status == "created",
            Message.conversation_id == event.thread_id,
        )
    )
    if abandoned is not None and sent_text:
        abandoned.status = "discarded"
        abandoned.final_sent_text = sent_text
        abandoned.sent_at = now
        summary.drafts_discarded += 1


def _ingest_sent_message(
    db: Session,
    connection: MailboxConnection,
    event: MailboxEvent,
    summary: SyncSummary,
    now: datetime,
    address_index: dict[str, str],
) -> None:
    """A manually sent email to a known candidate becomes a CRM row.

    Sequence sends are recognised by their native id (stored at send time) and
    skipped; mail to strangers is skipped without storing anything.
    """
    if not connection.thread_sync_enabled:
        return
    if _already_recorded(db, org_id=connection.org_id, native_id=event.native_id):
        return
    recipients = list(event.to_addresses)
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
        event=event,
        now=now,
        source=connection.provider,
    )
    summary.sent_ingested += 1


def _enqueue_reply_draft(
    db: Session, *, connection: MailboxConnection, native_id: str
) -> None:
    """Queue drafting for one recorded reply. Opt-outs and automation get none."""
    from ..config import get_settings
    from ..jobs import KIND_REPLY_DRAFT, enqueue
    from .reply_drafting import NO_DRAFT_INTENTS

    # Off by default. Gated here so nothing is queued, and again in the worker
    # so anything already queued when it was switched off stays unrun.
    if not get_settings().reply_drafting_enabled:
        return

    inbound = db.scalar(
        select(Message).where(
            Message.org_id == connection.org_id,
            Message.direction == "inbound",
            Message.provider_native_id == native_id,
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
