"""Group-chat referrals: answer once, wait for the newcomer, then leave.

A candidate creates an iMessage group with our line and a friend in it. That is
the whole referral gesture — three taps, no form, no code to remember — and this
module is what happens next.

**We never open the friend's conversation.** They did not consent to hear from
us; a mutual contact added them. So the reward is not paid on the group existing,
it is paid when the friend texts us *themselves*, which makes their own first
message both the confirmation and the consent. That single decision is why there
is no outbound-to-a-stranger path anywhere in this file, and why
``confirm_pending`` lives on the ordinary inbound route rather than on a timer.

What we do send is one message into the group we were invited into. It reaches
the friend's device, and they did not ask for it — the residual risk of the
feature, and it is bounded deliberately:

* **One thank-you per chat, ever**, and at most one redirect after it. Two
  outbound messages is the ceiling; past that we are quiet.
* **No link.** Linq forbids links in a first outbound, and a group can silently
  be on SMS where a link is a spam signal.
* **We say we are leaving before we do, and then we wait.** The first message
  promises to step out once the newcomer has messaged us, and ``depart``
  honours that on their first private text -- not on the answer. Leaving in the
  same breath as handing somebody a number gives them no way to check they had
  the right one, and a group we have walked out of is one they cannot ask in.
  A group with nobody new in it is the exception: nothing to wait for, so we go.
* **A suppressed number is dropped before it is ever counted**, and STOP inside
  a group suppresses exactly as it does anywhere else.

Staying has one structural cost, paid in ``threads.find_active_thread``: while
we are in the chat the group thread is ``active`` and carries the *referrer's*
number, so that query excludes ``MODE_REFERRAL`` outright. Without it their next
private message resumes the group thread and every reply to them lands in front
of the friend they just introduced. That exclusion is load-bearing.

The intake agent never runs in a group. It asks one person about their own
preferences, and there is more than one person in the room.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..intake.phones import lookup_key
from ..intake.service import resolve_candidate_id
from ..models import IntakeThread, Referral, utcnow
from . import prompts
from .channels import LINQ, OutboundMessage
from .linq import ChatHandle, fetch_chat_handles, leave_chat
from .suppression import address_has_opted_out
from .threads import MODE_REFERRAL, STATUS_COMPLETE, close_thread

logger = logging.getLogger(__name__)

#: Re-exported. ``MODE_REFERRAL`` is defined in ``threads`` so the thread query
#: that has to exclude it can see it, but this is the module that owns the idea.
__all__ = ["MODE_REFERRAL"]

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"

REASON_ALREADY_A_CANDIDATE = "already_a_candidate"
REASON_SUPPRESSED = "suppressed"

# How many newcomers one group can be worth. A referral is a personal vouch;
# somebody who adds nine strangers to a chat is doing something else, and the
# cap keeps that from being profitable without needing to judge intent.
MAX_REFEREES_PER_GROUP = 3


def address_key(handle: str) -> str:
    """The stored form of a handle: a phone key, or the handle as given.

    ``lookup_key`` refuses to guess a country and returns empty for anything it
    cannot parse — which is correct for numbers and useless for the Apple ID
    email addresses iMessage also delivers. Falling back to the raw handle keeps
    those matchable, and is why the columns are 255 wide.

    Both sides of a referral go through here, so confirmation compares like with
    like. Comparing a raw number against a stored key silently matches nothing,
    which the ``resolve_candidate_id`` docstring warns about at length.
    """
    handle = (handle or "").strip()
    return lookup_key(handle) or handle.lower()


def referees_from_handles(
    handles: tuple[ChatHandle, ...], *, referrer_address: str
) -> list[str]:
    """Who in this chat might be a newcomer, in payload order.

    Drops our own line, anybody who has already left or been removed, and the
    referrer. What is left is a candidate list, not yet a referral — whether
    each one counts is decided against the database in ``record_group``.
    """
    referrer = address_key(referrer_address)
    found: list[str] = []
    for entry in handles:
        if entry.is_me or not entry.is_active:
            continue
        key = address_key(entry.handle)
        if not key or key == referrer or key in found:
            continue
        found.append(key)
    return found


def record_group(
    db: Session,
    *,
    thread: IntakeThread,
    handles: tuple[ChatHandle, ...],
) -> list[Referral]:
    """Turn a group's participants into referral rows. Returns what was written.

    A row is written for every newcomer, including the ones that do not count —
    ``rejected`` with a reason rather than nothing at all. "Why did my friend
    not count" has to be answerable, and a referral that silently vanished is
    what makes people stop trusting the incentive.
    """
    settings = get_settings()
    bonus = max(settings.referral_bonus_applications, 0)
    referrer_address = address_key(thread.from_address)
    written: list[Referral] = []

    for referee in referees_from_handles(handles, referrer_address=referrer_address)[
        :MAX_REFEREES_PER_GROUP
    ]:
        existing = db.scalar(
            select(Referral).where(
                Referral.org_id == thread.org_id,
                Referral.referee_address == referee,
            )
        )
        if existing is not None:
            # The unique constraint would refuse this anyway; checking first
            # turns "already referred" into a log line instead of an
            # IntegrityError that takes the whole webhook down with it.
            logger.info(
                "referral skipped, %s already referred (%s)", referee, existing.status
            )
            continue

        reason = _rejection_reason(db, org_id=thread.org_id, referee=referee)
        row = Referral(
            org_id=thread.org_id,
            referrer_candidate_id=thread.candidate_id,
            referrer_address=referrer_address,
            referee_address=referee,
            group_chat_id=thread.provider_thread_id,
            group_thread_id=thread.id,
            status=STATUS_REJECTED if reason else STATUS_PENDING,
            bonus_applications=0 if reason else bonus,
            reason=reason,
        )
        db.add(row)
        written.append(row)

    db.flush()
    return written


def _rejection_reason(db: Session, *, org_id: str, referee: str) -> str:
    """Why this handle cannot be a referral, or empty if it can.

    Both are disqualifying; the order only decides which reason is recorded. A
    number we already talk to is not a referral (it is an introduction to
    somebody we know), and one that has opted out is not a lead at any price.

    The sender is not checked here -- ``referees_from_handles`` has already
    dropped them, so a self-referral never gets as far as a row. The residual
    case is a referrer whose handle in the chat differs from the number they
    texted from; if they are a candidate at all, the first check catches it.
    """
    if resolve_candidate_id(db, org_id=org_id, phone=referee) is not None:
        return REASON_ALREADY_A_CANDIDATE
    if address_has_opted_out(db, channel=LINQ, from_address=referee):
        return REASON_SUPPRESSED
    return ""


def _our_number(thread: IntakeThread) -> str:
    return get_settings().linq_from_number or thread.to_address


def answer_group(db: Session, *, thread: IntakeThread, rows: list[Referral]) -> bool:
    """Send the group its one message. True if it sent.

    **We do not leave yet when there is somebody to wait for.** The newcomer
    has just been told to text us and has not done it; walking out of the room
    in the same breath as asking would leave them holding a number with no way
    to check they had the right one. So the first message gives the heads-up --
    "once you've messaged me i'll step out" -- and ``depart`` honours it later,
    on the friend's own first text.

    The thread therefore stays ``active``, which is only safe because
    ``find_active_thread`` excludes ``MODE_REFERRAL``. Without that exclusion
    this thread carries the referrer's number and would be resumed for their
    next private message, delivering the reply into the group.

    A group with nobody new in it is the exception: there is no one to wait
    for, so we say so and go straight away.
    """
    from .conversation import deliver

    pending = [r for r in rows if r.status == STATUS_PENDING]
    if pending:
        body = prompts.REFERRAL_GROUP_THANKS.format(
            recruiter=prompts.recruiter_name(),
            number=_our_number(thread),
            bonus=pending[0].bonus_applications,
        )
    else:
        # Nobody new, or Linq would not tell us who is here. Still answered:
        # being added to a group and saying nothing reads as broken. Promises
        # nothing, because there is no referral to pay for.
        body = prompts.REFERRAL_GROUP_KNOWN_ONLY.format(
            recruiter=prompts.recruiter_name(),
            number=_our_number(thread),
        )

    row = deliver(db, thread=thread, message=OutboundMessage(body=body))
    if not pending:
        _leave_and_close(db, thread=thread, reason="referral group, nobody new")
    return row is not None and row.status != "failed"


def redirect_group(db: Session, *, thread: IntakeThread) -> bool:
    """Somebody is still talking in the group. Point them at their own thread.

    Sent at most once. After the answer and one redirect the number has been
    given twice, and a third message into a chat nobody asked us to join is us
    not taking the hint -- so we go quiet and stay quiet, still in the room
    because the newcomer may yet text.

    Never runs a turn. Whatever was asked, the answer is the same: not here.
    """
    from .conversation import deliver

    if thread.outbound_count >= prompts.MAX_GROUP_REPLIES:
        logger.info("group %s already redirected; staying quiet", thread.id)
        return False
    body = prompts.REFERRAL_GROUP_REDIRECT.format(number=_our_number(thread))
    row = deliver(db, thread=thread, message=OutboundMessage(body=body))
    return row is not None and row.status != "failed"


def depart(db: Session, *, thread: IntakeThread) -> None:
    """Say goodbye and get out. Called once the newcomer has texted us.

    The sign-off exists because what triggers this -- a private message -- is
    invisible from inside the group, so leaving without a word would look like
    we lost interest. It names nobody and says nothing about why: the group may
    hold people with no business knowing either.
    """
    from .conversation import deliver

    body = prompts.REFERRAL_GROUP_SIGN_OFF.format(number=_our_number(thread))
    deliver(db, thread=thread, message=OutboundMessage(body=body))
    _leave_and_close(db, thread=thread, reason="referral confirmed, left the group")


def _leave_and_close(db: Session, *, thread: IntakeThread, reason: str) -> None:
    """Out of the chat, and the thread terminal. Best-effort, in that order.

    ``leave_chat`` never raises and the thread closes either way: a failed
    leave with the thread left open would let this group be answered a second
    time, and a duplicate thank-you is worse than lingering in a chat.
    """
    leave_chat(thread.provider_thread_id)
    close_thread(db, thread=thread, status=STATUS_COMPLETE, reason=reason)


def handle_group(db: Session, *, thread: IntakeThread) -> dict:
    """The whole group-chat turn: look up the room, record it, answer, leave.

    Runs in the worker rather than the webhook because it is three network
    calls and Linq wants its 200 inside ten seconds — the same reasoning
    ``KIND_INTAKE_ATTACHMENT`` is queued for.
    """
    # Keyed on having spoken, not on the thread being closed. A group we are
    # still sitting in stays open by design now, so status alone no longer
    # answers "have we replied" -- and replaying the thank-you is the one
    # outcome worse than staying quiet.
    if thread.outbound_count > 0 or thread.status == STATUS_COMPLETE:
        return {"skipped": "group already answered"}

    handles = fetch_chat_handles(thread.provider_thread_id)
    if not handles:
        logger.info(
            "linq chat %r returned no participants; answering without a referral",
            thread.provider_thread_id,
        )
    # `rows`, not `referrals`: this is the referrals module, and a local of
    # that name shadows it for anything added below.
    rows = record_group(db, thread=thread, handles=handles)
    sent = answer_group(db, thread=thread, rows=rows)
    return {
        "sent": sent,
        "pending": [r.id for r in rows if r.status == STATUS_PENDING],
        "rejected": [(r.id, r.reason) for r in rows if r.status == STATUS_REJECTED],
    }


def confirm_pending(
    db: Session, *, org_id: str, address: str, thread: IntakeThread
) -> Referral | None:
    """The friend just texted us. Settle the referral that was waiting for them.

    Called from the ordinary inbound route the moment a Linq thread is opened,
    which is the only signal that matters: they reached us on their own.
    Returns the confirmed row, or None when this number was never referred.

    Confirms once. A second message finds no pending row, so the referrer is
    not thanked twice for the same friend.
    """
    key = address_key(address)
    if not key:
        return None
    referral = db.scalar(
        select(Referral).where(
            Referral.org_id == org_id,
            Referral.referee_address == key,
            Referral.status == STATUS_PENDING,
        )
    )
    if referral is None:
        return None

    referral.status = STATUS_CONFIRMED
    referral.confirmed_at = utcnow()
    referral.referee_thread_id = thread.id
    if not referral.referrer_candidate_id:
        # They may not have had a candidate row when they made the intro --
        # a referrer part-way through onboarding does not. Resolve it now, or
        # the bonus is stranded on a row nothing can attribute and they never
        # hear that their intro landed.
        referral.referrer_candidate_id = resolve_candidate_id(
            db, org_id=org_id, phone=referral.referrer_address
        )
    db.flush()

    _thank_referrer(db, referral=referral)
    _depart_group(db, referral=referral)
    logger.info(
        "referral %s confirmed: %s texted in", referral.id, referral.referee_address
    )
    return referral


def _depart_group(db: Session, *, referral: Referral) -> None:
    """Honour the promise made in the group: they messaged, so we step out.

    Only now, and only because of this. Staying was the point -- the newcomer
    was told to text us and needed us reachable in the meantime -- and this is
    the event that ends it. A group already left, or one whose thread has gone,
    is nothing to do again.
    """
    if not referral.group_thread_id:
        return
    group = db.get(IntakeThread, referral.group_thread_id)
    if group is None or group.status == STATUS_COMPLETE:
        return
    depart(db, thread=group)


def _thank_referrer(db: Session, *, referral: Referral) -> None:
    """Tell the referrer their intro landed, if they have a line to tell.

    Routed through ``notify_candidate`` so quiet hours, the org circuit breaker
    and the suppression checks all apply — this is an unprompted message, and
    the fact that it carries good news does not exempt it. No manager thread
    means no text; the row stands either way, and the bonus is not lost.
    """
    from .notify import EVENT_REFERRAL_CONFIRMED, notify_candidate

    if not referral.referrer_candidate_id:
        return
    notify_candidate(
        db,
        org_id=referral.org_id,
        candidate_id=referral.referrer_candidate_id,
        event=EVENT_REFERRAL_CONFIRMED,
        subject_id=referral.id,
    )


def earned_bonus(db: Session, *, org_id: str, candidate_id: str) -> int:
    """Extra applications this candidate has banked from confirmed referrals.

    Nothing spends this yet. ``applications.limits.may_submit`` still compares
    against ``SUBMISSIONS_PER_WEEK`` alone, and wiring this in is a deliberate
    one-line follow-up rather than part of this change — it means deciding what
    the paid tier is, and the group copy says "banked" precisely because that
    decision has not been made.
    """
    if not candidate_id:
        return 0
    return (
        db.scalar(
            select(func.coalesce(func.sum(Referral.bonus_applications), 0)).where(
                Referral.org_id == org_id,
                Referral.referrer_candidate_id == candidate_id,
                Referral.status == STATUS_CONFIRMED,
            )
        )
        or 0
    )


def confirmed_for(
    db: Session, *, org_id: str, candidate_id: str
) -> list[tuple[str, datetime | None]]:
    """(referee address, when it confirmed) for one referrer. For surfacing."""
    rows = db.scalars(
        select(Referral)
        .where(
            Referral.org_id == org_id,
            Referral.referrer_candidate_id == candidate_id,
            Referral.status == STATUS_CONFIRMED,
        )
        .order_by(Referral.confirmed_at.desc())
    ).all()
    return [(row.referee_address, row.confirmed_at) for row in rows]
