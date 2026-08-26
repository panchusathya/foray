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
from ..jobmatch.ids import source_id_for
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
from . import apply_link, identity, intents, prompts
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

    # Corrections keep flowing into the preference whatever the topic is --
    # including contact details. Without this a thread that reached `managing`
    # without a name could never acquire one: the manager would ask, the
    # candidate would answer, and the answer would go nowhere, so every reply
    # to "what's your name" was met with the same request again.
    identity.absorb(
        thread,
        body=last.body or "",
        asked_field=thread.last_field_asked or "",
    )
    _reread(db, thread=thread)
    # force=True, unlike intake. Past the handover there is already a candidate
    # and a preference row, and the only question is whether this row stays
    # current -- "actually, make that 260" has to reach matching whatever the
    # coverage gate thinks. Unforced, `_capture` returns early on an open gate,
    # and the gate for a text-onboarded candidate is open by design now that
    # onboarding stops after the profile confirm.
    _capture(db, thread=thread, force=True)
    if not thread.candidate_id:
        identity.link_candidate(db, thread=thread)
    state = coverage(thread.record or {})

    # A link that arrived before we knew who was texting is spent the moment
    # a candidate exists -- idempotent, so re-entered turns cannot double it.
    apply_link.ensure_enqueued(db, thread)

    presented = list(thread.last_presented or [])
    intent = intents.classify(last.body or "", presented=presented)

    def _say(body: str, action: str = ACTION_ANSWERED, *, verbatim: bool = False):
        # verbatim copy names companies and roles read out of the database --
        # rewriting it risks changing a fact, so only the conversational lines
        # go through the composer.
        if not verbatim:
            from .conversation import humanize

            body = humanize(thread, says=body, last_inbound=last.body or "")
        row = _deliver(
            db, thread=thread, message=OutboundMessage(body=body, verbatim=verbatim)
        )
        return TurnResult(action, thread, state, sent=[r for r in [row] if r])

    if intent.kind == intents.INTENT_APPLY_LINK:
        return _apply_by_link(db, thread=thread, last=last, state=state, url=intent.url)
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
    if intent.kind == intents.INTENT_AFFIRM:
        # "yes" almost always means yes to the thing we just offered, and the
        # thing we offer is roles.
        return present_matches(db, thread=thread, state=state)
    if intent.kind == intents.INTENT_DECLINE:
        return _say(_fresh(db, thread, prompts.MANAGER_ACK_DECLINE, prompts.MANAGER_STANDING_BY))
    if intent.kind == intents.INTENT_PROFILE_ANSWER:
        return _say(_fresh(db, thread, prompts.MANAGER_NOTED, prompts.MANAGER_NOTED_AGAIN))
    return _say(_fresh(db, thread, prompts.MANAGER_FALLBACK, prompts.MANAGER_FALLBACK_AGAIN))


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


def _apply_by_link(db: Session, *, thread: IntakeThread, last: IntakeMessage, state, url: str):
    """A job URL in the message: the posting and the per-application yes at once.

    The link is stashed before anything else. Identity may still be missing,
    and the stash is what lets the answer to "what name goes on this?" finish
    the job the link started -- ``ensure_enqueued`` runs at the top of every
    managing turn, so the reply that links a candidate spends it.
    """
    from .conversation import TurnResult, _deliver

    apply_link.stash(thread, url=url, message_id=last.id)

    candidate_id = thread.candidate_id or identity.link_candidate(db, thread=thread)
    if not candidate_id:
        ask, field = _identity_request(thread)
        body = prompts.LINK_NEED_IDENTITY.format(ask=ask[0].lower() + ask[1:])
        row = _deliver(
            db, thread=thread, message=OutboundMessage(body=body, asked_field=field)
        )
        return TurnResult("asked", thread, state, sent=[r for r in [row] if r])

    # Already in flight for this exact URL: say so now rather than promising
    # a package the worker would refuse a minute later.
    posting = db.scalar(
        select(JobPosting).where(
            JobPosting.org_id == thread.org_id,
            JobPosting.source_id == source_id_for(url=url),
        )
    )
    if posting is not None:
        existing = db.scalar(
            select(Application).where(
                Application.candidate_id == candidate_id,
                Application.job_posting_id == posting.id,
            )
        )
        if existing is not None:
            apply_link.clear(thread)
            row = _deliver(
                db, thread=thread, message=OutboundMessage(body=prompts.NOTIFY_LINK_DUPLICATE)
            )
            return TurnResult(ACTION_ANSWERED, thread, state, sent=[r for r in [row] if r])

    apply_link.ensure_enqueued(db, thread)
    row = _deliver(db, thread=thread, message=OutboundMessage(body=prompts.LINK_ACK))
    return TurnResult(ACTION_APPLIED, thread, state, sent=[r for r in [row] if r])


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


def _request_discovery(db: Session, *, thread: IntakeThread, candidate_id: str) -> bool:
    """Kick off a search for this candidate on request. True when one is queued.

    False for the two honest reasons: nobody to search for, or a spec too thin
    to search on (``queries_for`` refuses to spend a credit on "jobs"). Both
    keep the old canned line, which is accurate in those cases.
    """
    from ..jobmatch.discover_candidate import enqueue_discovery_on_request
    from ..models import Candidate, CandidatePreference

    candidate = db.get(Candidate, candidate_id)
    if candidate is None or candidate.org_id != thread.org_id:
        return False
    preference = db.scalar(
        select(CandidatePreference).where(
            CandidatePreference.org_id == thread.org_id,
            CandidatePreference.candidate_id == candidate_id,
        )
    )
    if preference is None:
        return False
    return enqueue_discovery_on_request(db, candidate=candidate, preference=preference) is not None


def _fresh(db, thread, first: str, second: str) -> str:
    """``first``, unless we just said it -- then ``second``.

    Two identical canned lines in a row is the clearest possible signal that
    nobody is home. The live thread sent "Noted — thanks. Anything else you
    want me looking for?" twice back to back, and the second one is what made
    the agent look broken rather than merely unhelpful.
    """
    from .threads import OUTBOUND, messages_of

    # Case-insensitive: the channel voice lowercases outbound copy before it
    # is stored, so comparing against the constant as written never matches.
    for message in reversed(messages_of(db, thread)):
        if message.direction != OUTBOUND:
            continue
        said = (message.body or "").strip().lower()
        return second if said == first.strip().lower() else first
    return first


def _identity_request(thread) -> tuple[str, str]:
    """What we still need before anyone can be matched, and the field to tag.

    Tagging the outbound with the field means the answer is absorbed on the
    next turn. Untagged, the candidate answers into a void and gets asked
    again -- which is exactly what happened in the first live thread.
    """
    need_name = not (thread.contact_name or "").strip()
    need_contact = not (thread.contact_email or "").strip() and not (
        thread.contact_linkedin or ""
    ).strip()

    if need_name and need_contact:
        return ("What name and email should I put on this?", identity.CONTACT_NAME)
    if need_name:
        return ("What name should I put on this?", identity.CONTACT_NAME)
    return ("What's the best email to reach you on?", identity.CONTACT_EMAIL)


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
        # Ask for what is actually missing. Demanding the email from somebody
        # who gave it two messages ago reads as not listening, and it was the
        # name we never captured.
        body, asked = _identity_request(thread)
        row = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(body=body, asked_field=asked),
            origin=origin,
        )
        return TurnResult("asked", thread, state, sent=[r for r in [row] if r])

    pairs = eligible_matches(db, org_id=thread.org_id, candidate_id=candidate_id)
    if not pairs:
        if quiet:
            return None
        # Asked directly and holding nothing: go and look before answering.
        # Reading the book we happen to have is the whole of what "find me
        # roles" used to do, so a candidate whose spec had never been searched
        # -- or whose spec was searched once, at handover, before any of it was
        # parsed -- got "nothing clears the bar" from a search that had never
        # run. Capped at one billed sweep per candidate per day.
        searching = _request_discovery(db, thread=thread, candidate_id=candidate_id)
        body = (
            "Looking now — I'll text you as soon as something worth your time turns up."
            if searching
            else "Nothing new clears the bar right now — I'll text you when something does."
        )
        row = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(body=body),
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
