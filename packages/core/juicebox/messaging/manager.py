"""What the recruiting manager does once onboarding is finished.

Onboarding itself is ``conversation._run_linkedin_short`` — intro, LinkedIn
scrape, confirm, one batched gap-fill. When it wraps up it hands the thread
here instead of completing it, because completing is silence: ``run_turn``
never speaks to a complete thread again, and a recruiting manager's line
going dead mid-search is a product failure, not an end state.

``conversation.run_turn`` stays the single entry point — its safety rails
(STOP, suppression, quiet hours, never two outbound in a row) run before this
module is reached, and every send still goes through ``_deliver``.

**managing** is intent-routed. Presenting roles writes ``JobProposal`` rows
and freezes them on ``last_presented`` so "apply to 2" and "the Goldman one"
resolve against exactly what was shown. An "apply" is the per-application
yes, bound to the message it arrived in. Extraction (``_reread``) still runs
every turn, so "actually, make that 260" keeps flowing into the preference
and the rescore loop no matter what the conversation is nominally about.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..applications import limits
from ..applications.bar import passes_bar
from ..applications.service import (
    ApplicationError,
    DuplicateApplicationError,
    application_status_summary,
    create_application,
    request_package,
)
from ..intake.schema import coverage
from ..models import (
    Application,
    IntakeMessage,
    IntakeThread,
    JobMatch,
    JobPosting,
    JobProposal,
    utcnow,
)
from ..portal.links import mint_portal_link
from ..presentation.jobcard import jobcard, render_text
from . import identity, intents, prompts
from .channels import OutboundMessage

logger = logging.getLogger(__name__)

MODE_INTAKE = "intake"
MODE_MANAGING = "managing"

# Which modes take proactive notifications. Onboarding is not one of them:
# the LinkedIn-short flow is mid-conversation and does not want a card deck
# arriving on top of its own question.
MANAGER_MODES = frozenset({MODE_MANAGING})

ACTION_PRESENTED = "presented"
ACTION_APPLIED = "applied"
ACTION_ANSWERED = "answered"

MAX_CARDS_PER_MESSAGE = 3


def run_manager_turn(db: Session, *, thread: IntakeThread, last: IntakeMessage):
    from .conversation import TurnResult

    if thread.mode == MODE_MANAGING:
        return _managing_turn(db, thread=thread, last=last)
    logger.warning("thread %s has unknown mode %r; holding", thread.id, thread.mode)
    return TurnResult("held", thread, coverage(thread.record or {}))


# ---------------------------------------------------------------------------
# managing: the steady state
# ---------------------------------------------------------------------------


def _managing_turn(db: Session, *, thread: IntakeThread, last: IntakeMessage):
    from .conversation import TurnResult, _capture, _deliver, _reread

    # Corrections keep flowing into the preference whatever the topic is.
    _reread(db, thread=thread)
    _capture(db, thread=thread)
    state = coverage(thread.record or {})

    presented = list(thread.last_presented or [])
    intent = intents.classify(last.body or "", presented=presented)

    def _say(body: str, action: str = ACTION_ANSWERED, *, verbatim: bool = False):
        row = _deliver(
            db, thread=thread, message=OutboundMessage(body=body, verbatim=verbatim)
        )
        return TurnResult(action, thread, state, sent=[r for r in [row] if r])

    if intent.kind == intents.INTENT_APPLY:
        return _apply(db, thread=thread, last=last, state=state, selection=intent.selection)
    if intent.kind == intents.INTENT_JOBS_MORE:
        return present_matches(db, thread=thread, state=state)
    if intent.kind == intents.INTENT_JOBS_DETAIL:
        return _detail(db, thread=thread, state=state, selection=intent.selection)
    if intent.kind == intents.INTENT_STATUS:
        return _say(
            application_status_summary(
                db, org_id=thread.org_id, candidate_id=thread.candidate_id or ""
            ),
            verbatim=True,
        )
    if intent.kind == intents.INTENT_NETWORKING:
        return _say(_networking_text(db, thread), verbatim=True)
    if intent.kind == intents.INTENT_PROFILE_ANSWER:
        return _say("Noted — thanks. Anything else you want me looking for?")
    return _say(prompts.MANAGER_FALLBACK)


def _apply(db: Session, *, thread: IntakeThread, last: IntakeMessage, state, selection: int):
    from .conversation import TurnResult, _deliver

    presented = list(thread.last_presented or [])
    if not presented:
        return present_matches(db, thread=thread, state=state)
    if not 1 <= selection <= len(presented):
        row = _deliver(db, thread=thread, message=OutboundMessage(body=prompts.CLARIFY_WHICH))
        return TurnResult("clarified", thread, state, sent=[r for r in [row] if r])

    entry = presented[selection - 1]
    posting = db.get(JobPosting, entry.get("posting_id") or "")
    proposal = db.get(JobProposal, entry.get("proposal_id") or "")
    if posting is None:
        row = _deliver(
            db, thread=thread, message=OutboundMessage(body=prompts.DETAIL_UNAVAILABLE)
        )
        return TurnResult("clarified", thread, state, sent=[r for r in [row] if r])

    candidate_id = thread.candidate_id or identity.link_candidate(db, thread=thread)
    if not candidate_id:
        row = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(
                body="I need your name and email before I can apply for you — what's the best email?",
                asked_field=identity.CONTACT_EMAIL,
            ),
        )
        return TurnResult("asked", thread, state, sent=[r for r in [row] if r])

    if proposal is not None and proposal.status == "proposed":
        proposal.status = "accepted"
        proposal.responded_at = utcnow()
        proposal.responded_message_id = last.id

    body = prompts.APPLY_ACK.format(title=posting.title, company=posting.company)
    action = ACTION_APPLIED
    try:
        application = create_application(
            db,
            org_id=thread.org_id,
            candidate_id=candidate_id,
            job_posting_id=posting.id,
            source="candidate",
            proposal_id=proposal.id if proposal is not None else None,
            # The per-application yes, bound to the message it arrived in.
            consenting_message_id=last.id,
            consent_verbatim=(last.body or "")[:400],
        )
        request_package(db, application)
    except DuplicateApplicationError:
        body = (
            f"You're already in flight for {posting.title} at {posting.company} — "
            'ask me "status" any time.'
        )
        action = ACTION_ANSWERED
    except ApplicationError:
        body = prompts.APPLY_PARKED.format(title=posting.title, company=posting.company)
        action = ACTION_ANSWERED

    row = _deliver(db, thread=thread, message=OutboundMessage(body=body))
    return TurnResult(action, thread, state, sent=[r for r in [row] if r])


def _detail(db: Session, *, thread: IntakeThread, state, selection: int):
    from .conversation import TurnResult, _deliver

    presented = list(thread.last_presented or [])
    if not presented or not 1 <= selection <= len(presented):
        body = prompts.CLARIFY_WHICH if presented else prompts.MANAGER_FALLBACK
        row = _deliver(db, thread=thread, message=OutboundMessage(body=body))
        return TurnResult("clarified", thread, state, sent=[r for r in [row] if r])

    entry = presented[selection - 1]
    posting = db.get(JobPosting, entry.get("posting_id") or "")
    if posting is None:
        row = _deliver(
            db, thread=thread, message=OutboundMessage(body=prompts.DETAIL_UNAVAILABLE)
        )
        return TurnResult("clarified", thread, state, sent=[r for r in [row] if r])

    bits = [f"{posting.title} at {posting.company}"]
    if posting.location:
        bits.append(posting.location)
    excerpt = " ".join((posting.jd_text or "").split())[:220]
    if excerpt:
        bits.append(excerpt + ("…" if len(posting.jd_text or "") > 220 else ""))
    if posting.source_url:
        bits.append(posting.source_url)
    body = ". ".join(bits[:2]) + ("\n" + "\n".join(bits[2:]) if len(bits) > 2 else "")
    body += f'\nSay "apply to {selection}" and I\'ll get it ready.'
    row = _deliver(db, thread=thread, message=OutboundMessage(body=body))
    return TurnResult(ACTION_ANSWERED, thread, state, sent=[r for r in [row] if r])


def _networking_text(db: Session, thread: IntakeThread) -> str:
    from ..applications.networking import refresh_networking

    application = db.scalar(
        select(Application)
        .where(
            Application.org_id == thread.org_id,
            Application.candidate_id == (thread.candidate_id or ""),
        )
        .order_by(Application.updated_at.desc())
    )
    if application is None:
        return "Once you've applied somewhere I can suggest people to talk to there."
    entries = list(application.networking or []) or refresh_networking(db, application)
    if not entries:
        return "No warm paths at that company yet — I'll keep looking."
    lines = ["Worth talking to:"]
    for entry in entries[:2]:
        basis = "works there" if entry.get("basis") == "current" else "used to work there"
        title = f", {entry['title']}" if entry.get("title") else ""
        lines.append(f"• {entry.get('name', '')}{title} — {basis}")
    link = mint_portal_link(
        db,
        org_id=thread.org_id,
        candidate_id=thread.candidate_id,
        path=f"/applications/{application.id}",
        channel=thread.channel,
    )
    lines.append(f"Intro drafts you can copy: {link}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# presenting roles (also called by the notify path)
# ---------------------------------------------------------------------------


def eligible_matches(
    db: Session, *, org_id: str, candidate_id: str, limit: int = MAX_CARDS_PER_MESSAGE
) -> list[tuple[JobMatch, JobPosting]]:
    """Bar-passing, never-before-answered matches, best first."""
    answered = set(
        db.scalars(
            select(JobProposal.job_posting_id).where(
                JobProposal.org_id == org_id, JobProposal.candidate_id == candidate_id
            )
        ).all()
    )
    answered.update(
        db.scalars(
            select(Application.job_posting_id).where(
                Application.org_id == org_id, Application.candidate_id == candidate_id
            )
        ).all()
    )
    rows = db.execute(
        select(JobMatch, JobPosting)
        .join(JobPosting, JobPosting.id == JobMatch.job_posting_id)
        .where(
            JobMatch.org_id == org_id,
            JobMatch.candidate_id == candidate_id,
            JobMatch.status != "dismissed",
            JobPosting.status == "open",
        )
        .order_by(JobMatch.score.desc())
        .limit(100)
    ).all()
    pairs: list[tuple[JobMatch, JobPosting]] = []
    for match, posting in rows:
        if posting.id in answered:
            continue
        if passes_bar(match, posting).ok:
            pairs.append((match, posting))
            if len(pairs) >= limit:
                break
    return pairs


def present_matches(
    db: Session, *, thread: IntakeThread, state, origin: str = "reply", quiet: bool = False
):
    """Show up to three roles, write the proposal rows, freeze the frame.

    Budget-gated: the weekly proposal budget and the one-proposal-message-a-day
    cap both apply. Asked directly, a refusal sends a graceful one-liner —
    silence from a budget looks identical to being ignored. On the proactive
    path (``quiet=True``) the refusal IS silence: an unprompted "nothing new"
    text spends attention on nothing.
    """
    from .conversation import TurnResult, _deliver

    candidate_id = thread.candidate_id or identity.link_candidate(db, thread=thread)
    if not candidate_id:
        if quiet:
            return None
        row = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(
                body="Tell me your name and email first so I can match you properly."
            ),
            origin=origin,
        )
        return TurnResult("asked", thread, state, sent=[r for r in [row] if r])

    pairs = eligible_matches(db, org_id=thread.org_id, candidate_id=candidate_id)
    if not pairs:
        if quiet:
            return None
        row = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(
                body="Nothing new clears the bar right now — I'll text you when something does."
            ),
            origin=origin,
        )
        return TurnResult(ACTION_ANSWERED, thread, state, sent=[r for r in [row] if r])

    decision = limits.may_propose(
        db, org_id=thread.org_id, candidate_id=candidate_id, count=len(pairs)
    )
    if not decision:
        if quiet:
            return None
        row = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(
                body="I've got roles lined up — I'll send them tomorrow so I'm not flooding you."
            ),
            origin=origin,
        )
        return TurnResult(ACTION_ANSWERED, thread, state, sent=[r for r in [row] if r])

    cards = [jobcard(posting, match) for match, posting in pairs]
    link = mint_portal_link(
        db, org_id=thread.org_id, candidate_id=candidate_id, channel=thread.channel
    )
    body = render_text(
        [{k: v for k, v in card.items() if k != "recruiter_only"} for card in cards],
        footer=f"Full cards: {link}",
    )[: prompts.MAX_CARD_MESSAGE_CHARS]

    # verbatim: the card quotes employers and role titles out of a posting.
    # Those are somebody else's proper nouns, not our lowercase voice.
    row = _deliver(
        db,
        thread=thread,
        message=OutboundMessage(body=body, verbatim=True),
        origin=origin,
    )

    frame = []
    now = utcnow()
    for (match, posting), card in zip(pairs, cards, strict=True):
        proposal = JobProposal(
            org_id=thread.org_id,
            candidate_id=candidate_id,
            job_posting_id=posting.id,
            job_match_id=match.id,
            channel=thread.channel,
            thread_id=thread.id,
            message_id=row.id if row is not None else None,
            status="proposed",
            reasons=card["reasons"],
        )
        db.add(proposal)
        db.flush()
        frame.append(
            {
                "proposal_id": proposal.id,
                "posting_id": posting.id,
                "company": posting.company,
                "title": posting.title,
            }
        )
    thread.last_presented = frame
    thread.presented_at = now
    db.flush()
    return TurnResult(ACTION_PRESENTED, thread, state, sent=[r for r in [row] if r])
