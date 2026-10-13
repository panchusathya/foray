"""Seed tests: copy a real email to a seed inbox, then go and look for it.

A seed test spends the same reputation a real send does, so it obeys every
rail a real send obeys, in the same order the sequencer applies them: the org
kill switch, the sending window, the mailbox's daily cap (which counts seed
sends -- rotation.py reads ``seed_tests``), and the domain's send gap. A test
that cannot go now *defers*; one that should not go at all is *skipped*.

What is sent is one of that mailbox's own emails from the last two weeks,
first emails preferred, re-rendered from its step with the AI fragments the
original used -- so the words, links and layout are what candidates got, and
no model is called. If re-rendering is not possible without a model, the
stored text goes instead: a slightly less faithful test beats no test.
Tracking, if the sequence has it on, points at the seed test's own id; it must
never register an open on the candidate's real message.

The row is created when the test is *queued* (by the tick or by "Test now"),
which is what makes the cadence and the retry guard simple: a mailbox with a
row inside the interval is not due, and a retried job finds its row already
``sent`` and stops.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..jobs import KIND_SEED_SEND, enqueue
from ..models import (
    MailboxConnection,
    Message,
    Org,
    SeedInbox,
    SeedTest,
    SequenceStep,
)
from .seeds import Probe, SeedReadError, reader_for

logger = logging.getLogger(__name__)

SEND_KIND = KIND_SEED_SEND
SOURCE_LOOKBACK = timedelta(days=14)
MISSING_AFTER = timedelta(hours=24)
STOP_LOOKING_AFTER = timedelta(hours=72)
#: The cadences the Deliverability page offers, in hours. 0 = off.
INTERVALS = (0, 12, 24, 48, 168)
#: Statuses that mean the provider accepted the send (so it spent cap).
SENT_STATUSES = ("sent", "inbox", "other_tab", "spam", "missing")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def mailbox_off_reason(connection: MailboxConnection, now: datetime | None = None) -> str | None:
    from ..models import utcnow
    from ..outreach.rotation import _can_send_via
    from .throttle import is_resting

    if connection.status != "active":
        return "mailbox_inactive"
    if not connection.send_enabled:
        return "send_disabled"
    if connection.daily_send_cap <= 0:
        return "capped_off"
    if not _can_send_via(connection.provider):
        return "provider_unconfigured"
    if is_resting(connection, now or utcnow()):
        # Resting sends nothing at all, seed tests included; a throttled
        # mailbox is still tested -- that is how it shows it recovered.
        return "resting"
    return None


def active_seeds(db: Session, org_id: str) -> list[SeedInbox]:
    return list(
        db.scalars(
            select(SeedInbox)
            .where(SeedInbox.org_id == org_id, SeedInbox.status == "active")
            .order_by(SeedInbox.address)
        )
    )


def _seed_order(db: Session, connection: MailboxConnection, seeds: list[SeedInbox], now):
    """Least-tested seed first (last 7 days), then least recently, then by address."""
    since = now - timedelta(days=7)
    counts = dict(
        db.execute(
            select(SeedTest.seed_id, func.count(SeedTest.id))
            .where(SeedTest.mailbox_id == connection.id, SeedTest.created_at >= since)
            .group_by(SeedTest.seed_id)
        ).all()
    )
    last = dict(
        db.execute(
            select(SeedTest.seed_id, func.max(SeedTest.created_at))
            .where(SeedTest.mailbox_id == connection.id)
            .group_by(SeedTest.seed_id)
        ).all()
    )
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return sorted(
        seeds,
        key=lambda s: (counts.get(s.id, 0), _aware(last.get(s.id)) or epoch, s.address),
    )


def due_tests(db: Session, org: Org, now: datetime) -> list[tuple[MailboxConnection, SeedInbox]]:
    """(mailbox, seed) for every mailbox whose scheduled test is due."""
    from ..outreach.rotation import eligible_mailboxes

    interval = int(getattr(org, "seed_test_interval_hours", 24) or 0)
    if interval <= 0:
        return []
    seeds = active_seeds(db, org.id)
    if not seeds:
        return []
    since = now - timedelta(hours=interval)
    out = []
    for connection in eligible_mailboxes(db, org_id=org.id, include_throttled=True):
        if mailbox_off_reason(connection, now):
            continue
        recent = db.scalar(
            select(SeedTest.id).where(
                SeedTest.mailbox_id == connection.id, SeedTest.created_at > since
            )
        )
        if recent is not None:
            continue
        out.append((connection, _seed_order(db, connection, seeds, now)[0]))
    return out


def queue_test(
    db: Session, *, connection: MailboxConnection, seed: SeedInbox, now: datetime, key: str
) -> SeedTest:
    """Create the queued row and its send job. Idempotent on ``key``."""
    test = db.scalar(
        select(SeedTest).where(SeedTest.org_id == connection.org_id, SeedTest.job_key == key)
    )
    if test is None:
        test = SeedTest(
            org_id=connection.org_id,
            mailbox_id=connection.id,
            seed_id=seed.id,
            job_key=key,
            from_address=connection.address,
            status="queued",
            created_at=now,
        )
        db.add(test)
        db.flush()
    enqueue(
        db,
        org_id=connection.org_id,
        kind=SEND_KIND,
        idempotency_key=key,
        payload={
            "org_id": connection.org_id,
            "mailbox_id": connection.id,
            "seed_id": seed.id,
            "key": key,
        },
        run_at=now,
    )
    return test


def queue_due(db: Session, org: Org, now: datetime) -> int:
    queued = 0
    for connection, seed in due_tests(db, org, now):
        key = f"seed:{connection.id}:{int(now.timestamp())}"
        queue_test(db, connection=connection, seed=seed, now=now, key=key)
        queued += 1
    return queued


def queue_test_now(db: Session, connection: MailboxConnection, now: datetime) -> dict:
    """One test to every active seed, as far as the mailbox's room allows."""
    from ..outreach.rotation import remaining_capacity

    seeds = active_seeds(db, connection.org_id)
    off = mailbox_off_reason(connection, now)
    if off:
        # Said out loud: "no room in the cap" would send someone to raise a
        # cap on a mailbox that is switched off or disconnected.
        return {"queued": 0, "seeds": len(seeds), "room": 0, "reason": off}
    waiting = set(
        db.scalars(
            select(SeedTest.seed_id).where(
                SeedTest.mailbox_id == connection.id, SeedTest.status == "queued"
            )
        )
    )
    room = max(0, remaining_capacity(db, connection=connection, now=now) - len(waiting))
    # A seed that already has a test on its way from this mailbox would only
    # give a second reading of the same thing, for another slot of the cap.
    fresh = [s for s in seeds if s.id not in waiting]
    if seeds and not fresh:
        return {"queued": 0, "seeds": len(seeds), "room": room, "reason": "already_queued"}
    chosen = _seed_order(db, connection, fresh, now)[:room]
    stamp = int(now.timestamp())
    for seed in chosen:
        queue_test(
            db,
            connection=connection,
            seed=seed,
            now=now,
            key=f"seed:{connection.id}:{seed.id}:now:{stamp}",
        )
    return {"queued": len(chosen), "seeds": len(seeds), "room": room}


# -- sending --------------------------------------------------------------------


def pick_source(db: Session, connection: MailboxConnection, now: datetime) -> Message | None:
    """This mailbox's most recent real email, first emails preferred."""
    rows = db.execute(
        select(Message, SequenceStep.step_order)
        .outerjoin(SequenceStep, SequenceStep.id == Message.step_id)
        .where(
            Message.mailbox_id == connection.id,
            Message.direction == "outbound",
            Message.source == "sequence",
            Message.status == "sent",
            Message.enrollment_id.is_not(None),
            Message.sent_at >= now - SOURCE_LOOKBACK,
        )
        .order_by(Message.sent_at.desc())
        .limit(50)
    ).all()
    if not rows:
        return None
    for message, order in rows:
        if order == 0:
            return message
    return rows[0][0]


def _render_body(db: Session, connection: MailboxConnection, source: Message) -> str:
    """The step's body as the candidate got it, or the stored text if that
    would need a model call."""
    from ..outreach.sequencer import build_message_content

    enrollment = source.enrollment
    step = db.get(SequenceStep, source.step_id) if source.step_id else None
    if enrollment is None or step is None:
        return source.body
    sequence = enrollment.sequence
    templates_text = f"{step.subject_template or ''}{step.body_template or ''}"
    if "{{ai" in templates_text.replace(" ", "") and not source.ai_fragments:
        return source.body
    if not enrollment.personalization and sequence.personalize:
        return source.body
    try:
        _subject, body, _context, _fragments = build_message_content(
            db,
            sequence=sequence,
            step=step,
            candidate=source.candidate,
            context=enrollment.personalization or None,
            enrollment=enrollment,
            prior_fragments=source.ai_fragments or None,
            connection=connection,
            now=_aware(source.sent_at),
        )
    except Exception:  # noqa: BLE001 - any render failure falls back to the stored text
        logger.info("seed test: could not re-render %s; using stored text", source.id)
        return source.body
    return body


def build_email(db: Session, *, connection, seed, source: Message, test: SeedTest):
    from ..outreach import templates
    from ..outreach.sender import OutboundEmail
    from ..security import signed_intake_token, signed_tracking_token

    settings = get_settings()
    sequence = source.enrollment.sequence if source.enrollment else None
    body = _render_body(db, connection, source)
    html_body = templates.add_tracking(
        templates.to_html(body),
        base_url=settings.base_url.rstrip("/"),
        message_id=test.id,
        token=signed_tracking_token(test.id),
        sign_click=lambda target: signed_intake_token(f"click:{test.id}", target),
        opens=bool(sequence and sequence.track_opens),
        clicks=bool(sequence and sequence.track_clicks),
    )
    return OutboundEmail(
        to=seed.address,
        subject=source.subject,
        text_body=templates.to_text(body),
        html_body=html_body,
        # Every provider sends as the mailbox itself; say so rather than
        # passing the sequence's address for the provider to override.
        from_email=connection.address,
        from_name=(sequence.from_name if sequence else "") or connection.display_name,
        headers={"X-Juicebox-Message-Id": test.id},
    )


def send_seed_test(
    db: Session,
    *,
    connection: MailboxConnection,
    seed: SeedInbox,
    now: datetime,
    key: str,
) -> dict:
    from ..outreach.rotation import frees_at, mailbox_gap_wait, remaining_capacity
    from ..outreach.sender import sender_for_connection
    from ..outreach.sequencer import note_mail_sent
    from ..outreach.window import org_window, window_wait

    test = db.scalar(
        select(SeedTest).where(SeedTest.org_id == connection.org_id, SeedTest.job_key == key)
    )
    if test is not None and test.status in SENT_STATUSES:
        return {"status": "sent", "test_id": test.id}

    def skip(reason: str) -> dict:
        if test is not None and test.status == "queued":
            test.status = "failed"
            test.error = reason
        return {"status": "skipped", "reason": reason}

    if seed.status != "active":
        return skip("seed_inactive")
    reason = mailbox_off_reason(connection, now)
    if reason:
        return skip(reason)
    org = db.get(Org, connection.org_id)
    if org is not None and org.outreach_paused_at is not None:
        return skip("org_paused")
    if org is not None:
        opens = window_wait(org_window(org), now)
        if opens is not None:
            return {"status": "deferred", "reason": "outside_hours", "defer_until": opens}
    if remaining_capacity(db, connection=connection, now=now) <= 0:
        return {
            "status": "deferred",
            "reason": "mailbox_full",
            "defer_until": frees_at(db, connection=connection, now=now),
        }
    gap = mailbox_gap_wait(db, connection, now)
    if gap is not None:
        return {"status": "deferred", "reason": "spacing", "defer_until": gap}
    source = pick_source(db, connection, now)
    if source is None:
        return skip("no_recent_send")

    if test is None:
        test = SeedTest(
            org_id=connection.org_id,
            mailbox_id=connection.id,
            seed_id=seed.id,
            job_key=key,
            status="queued",
            created_at=now,
        )
        try:
            with db.begin_nested():
                db.add(test)
                db.flush()
        except IntegrityError:
            test = db.scalar(
                select(SeedTest).where(
                    SeedTest.org_id == connection.org_id, SeedTest.job_key == key
                )
            )
            if test is None:
                raise
            if test.status in SENT_STATUSES:
                return {"status": "sent", "test_id": test.id}
    test.source_message_id = source.id
    test.subject = source.subject
    test.from_address = connection.address

    email = build_email(db, connection=connection, seed=seed, source=source, test=test)
    result = sender_for_connection(db, connection).send(email)
    if not result.ok:
        test.status = "failed"
        test.error = result.error or "send failed"
        db.flush()
        return {"status": "failed", "error": test.error, "test_id": test.id}
    test.status = "sent"
    test.error = ""
    test.sent_at = now
    test.internet_message_id = result.internet_message_id or ""
    note_mail_sent(db, connection.org_id, now, connection=connection)
    db.flush()
    return {"status": "sent", "test_id": test.id}


# -- reading --------------------------------------------------------------------


def pending_tests(db: Session, seed: SeedInbox, now: datetime) -> list[SeedTest]:
    return list(
        db.scalars(
            select(SeedTest)
            .where(
                SeedTest.seed_id == seed.id,
                SeedTest.status == "sent",
                SeedTest.sent_at >= now - STOP_LOOKING_AFTER,
            )
            .order_by(SeedTest.sent_at)
        )
    )


def check_seed(db: Session, seed: SeedInbox, now: datetime) -> dict:
    """Look for every pending test in one seed, in one session."""
    pending = pending_tests(db, seed, now)
    placed = 0
    missing = 0
    try:
        with reader_for(seed) as reader:
            seed.spam_visible = bool(reader.spam_visible)
            for test in pending:
                found = reader.find(
                    Probe(
                        from_address=test.from_address,
                        subject=test.subject,
                        message_id=test.internet_message_id,
                        not_before=_aware(test.sent_at),
                    )
                )
                test.checked_at = now
                if found.found:
                    test.status = found.placement
                    test.folder = found.folder
                    placed += 1
                elif now - _aware(test.sent_at) >= MISSING_AFTER:
                    test.status = "missing"
                    missing += 1
    except SeedReadError as exc:
        seed.status = "error"
        seed.error = str(exc)
        db.flush()
        return {"seed": seed.address, "error": str(exc)}
    if seed.status == "error":
        seed.status = "active"
    seed.error = ""
    seed.last_checked_at = now
    db.flush()
    return {"seed": seed.address, "checked": len(pending), "placed": placed, "missing": missing}


def seeds_to_check(db: Session, org_id: str, now: datetime) -> list[SeedInbox]:
    """Seeds with tests waiting to be found, plus errored seeds (to recover)."""
    with_pending = select(SeedTest.seed_id).where(
        SeedTest.org_id == org_id,
        SeedTest.status == "sent",
        SeedTest.sent_at >= now - STOP_LOOKING_AFTER,
    )
    return list(
        db.scalars(
            select(SeedInbox).where(
                SeedInbox.org_id == org_id,
                or_(SeedInbox.id.in_(with_pending), SeedInbox.status == "error"),
                SeedInbox.status != "paused",
            )
        )
    )
