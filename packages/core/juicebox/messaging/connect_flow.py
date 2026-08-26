"""Connect-your-apps onboarding: the offer, its state, and the canned copy.

The ``linkedin_flow`` pattern: this module is vocabulary the turn handler
reads, never a second conversation engine. The offer is the *profile ask* —
one message carrying the connect link and today's LinkedIn/resume alternative
— so declining costs zero extra turns: whatever the person does next (paste a
URL, attach a file, tap the link, answer with words) lands in a path that
already existed.

State is deliberately thin. "Offered" lives in flow meta; "pending" is
derived from the connection row's ``index_state``, because we cannot see a
link being tapped — only the OAuth callback tells us anything happened.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..connectors import configured_connectors
from ..models import CandidateConnection, IntakeThread
from .connect_links import mint_connect_link
from .linkedin_flow import flow_meta, update_flow_meta

# Pseudo-field on last_field_asked, namespaced like batch.confirm.
BATCH_CONNECT = "batch.connect"

# Flow-meta key: the offer went out. One offer per thread, ever — a repeated
# pitch to somebody who ignored it is the fastest way to read as spam.
OFFERED_KEY = "connect_offered"


def should_offer(db: Session, thread: IntakeThread) -> bool:
    """Offer exactly once, only when something is configured to offer, and
    never to a thread that already holds a connection (a re-opened link can
    connect without the offer ever being sent)."""
    if not configured_connectors():
        return False
    if flow_meta(thread).get(OFFERED_KEY):
        return False
    return not has_connection(db, thread)


def mark_offered(thread: IntakeThread) -> None:
    update_flow_meta(thread, **{OFFERED_KEY: True})


def has_connection(db: Session, thread: IntakeThread) -> bool:
    return (
        db.scalar(
            select(CandidateConnection.id).where(CandidateConnection.thread_id == thread.id)
        )
        is not None
    )


def connect_pending(db: Session, thread: IntakeThread) -> bool:
    """An index is queued or running — the same "wait for it" state a scrape
    or a parsing resume is, and it holds the turn the same way. The job's own
    completion (or its catch-everything failure path) is what answers."""
    rows = db.scalars(
        select(CandidateConnection).where(
            CandidateConnection.thread_id == thread.id,
            CandidateConnection.status == "active",
        )
    )
    return any(
        (row.index_state or {}).get("status") in ("queued", "running") for row in rows
    )


def offer_message(thread: IntakeThread) -> str:
    """The combined profile ask. One connect link; the alternative is worded
    per channel exactly the way the existing asks are (attach-here on Linq,
    upload link on web) so this replaces those messages rather than adding a
    turn before them."""
    from . import prompts
    from .channels import LINQ
    from .uploads import mint_upload_link

    labels = [connector.label.lower() for connector in configured_connectors()]
    if len(labels) > 1:
        apps = ", ".join(labels[:-1]) + " or " + labels[-1]
    else:
        apps = labels[0] if labels else "an app"
    link = mint_connect_link(thread)
    if thread.channel == LINQ:
        return prompts.CONNECT_OFFER_LINQ.format(apps=apps, connect_link=link)
    return prompts.CONNECT_OFFER_WEB.format(
        apps=apps, connect_link=link, upload_link=mint_upload_link(thread)
    )


def connect_failed_message() -> str:
    return (
        "couldn't pull much from that just now — no worries. "
        "mind answering a couple of quick questions so we can still match you?"
    )


def declined_preface() -> str:
    """They passed on the offer (or answered with words). Not a failure, so
    the copy does not apologise — it just moves on."""
    return "all good — a couple of quick things so we can actually match you:"
