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
    Candidate,
    IntakeMessage,
    IntakeThread,
    JobMatch,
    JobPosting,
    JobProposal,
    utcnow,
)
from ..portal.links import mint_portal_link
from ..presentation.jobcard import jobcard, render_card
from . import applylink, identity, intents, prompts
from .cards import card_media
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

# Intents that mean "put that aside, I want something else". Everything outside
# this set is treated as an answer to a question a pending apply-link request
# is waiting on -- because an email or a name classifies as nothing in
# particular, and routing it would reply "Noted, thanks" to somebody who is
# halfway through applying to a role they chose.
NAVIGATIONAL_INTENTS = frozenset(
    {
        intents.INTENT_JOBS_MORE,
        intents.INTENT_JOBS_DETAIL,
        intents.INTENT_STATUS,
        intents.INTENT_NETWORKING,
    }
)


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

    # A link plus apply words is a role we have never proposed, so there is no
    # ordinal for `_apply` to resolve against.
    if intent.kind == intents.INTENT_APPLY and intent.url:
        return _apply_to_link(db, thread=thread, last=last, state=state, url=intent.url)
    # A request already in flight is answered before the intent router, because
    # the reply that carries an email or a name classifies as nothing in
    # particular -- routing it would answer "Noted, thanks" to somebody who is
    # halfway through applying. NAVIGATIONAL_INTENTS is the escape hatch: an
    # explicit "show me other roles" is not an answer to our question.
    if intent.kind not in NAVIGATIONAL_INTENTS and applylink.pending(thread):
        resumed = _advance_apply_link(db, thread=thread, last=last, state=state, intent=intent)
        if resumed is not None:
            return resumed

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


def apply_to_proposal(db: Session, *, thread: IntakeThread, proposal_id: str) -> dict:
    """Apply to one named proposal. The entry point a tapback uses.

    ``_apply`` resolves an ordinal out of what was last presented, because a
    typed "apply to 2" is all we have to go on there. A reaction names its
    message directly, so the proposal is already known and re-deriving it from
    a position would only introduce a way to get it wrong -- the frame can
    have moved on by the time somebody scrolls back and taps an older card.
    """
    presented = list(thread.last_presented or [])
    index = next(
        (i for i, e in enumerate(presented, start=1) if e.get("proposal_id") == proposal_id),
        0,
    )
    if index == 0:
        # Reacted to a card from an older batch. The proposal is still the
        # truth; rebuild a one-entry frame so `_apply` has something to read.
        proposal = db.get(JobProposal, proposal_id)
        if proposal is None:
            return {"skipped": "proposal not found", "proposal_id": proposal_id}
        posting = db.get(JobPosting, proposal.job_posting_id)
        thread.last_presented = [
            {
                "proposal_id": proposal.id,
                "posting_id": proposal.job_posting_id,
                "company": posting.company if posting else "",
                "title": posting.title if posting else "",
            }
        ]
        db.flush()
        index = 1

    last = _last_inbound(db, thread)
    result = _apply(
        db,
        thread=thread,
        last=last,
        state=coverage(thread.record or {}),
        selection=index,
    )
    return {
        "thread_id": thread.id,
        "action": result.action,
        "proposal_id": proposal_id,
        "sent": len(result.sent),
    }


def _last_inbound(db: Session, thread: IntakeThread) -> IntakeMessage:
    """`_apply` reads `last.id` for the consent trail.

    A tapback IS the consent, and it is not an IntakeMessage -- so the message
    it reacted to stands in. That is the right row to point at: it is the card
    the person actually said yes to.
    """
    from .threads import messages_of

    rows = messages_of(db, thread)
    return rows[-1] if rows else None


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


# ---------------------------------------------------------------------------
# a role the candidate found themselves
# ---------------------------------------------------------------------------


def _apply_to_link(db: Session, *, thread: IntakeThread, last: IntakeMessage, state, url: str):
    """Somebody pasted a job link and asked us to apply to it.

    Read the page, name it back, then close the gate. The confirm is not
    politeness -- it is the only check that we read the page correctly, and it
    is why ``read_posting`` refuses to return half a posting.
    """
    from .conversation import TurnResult, _deliver

    existing = applylink.pending(thread)
    if existing.url == url and existing.posting_id:
        # Same link again. Carry on where we were rather than re-fetching the
        # page and asking the same question a second time.
        resumed = _advance_apply_link(db, thread=thread, last=last, state=state, intent=None)
        if resumed is not None:
            return resumed

    posting = applylink.ingest(db, org_id=thread.org_id, url=url)
    if posting is None:
        row = _deliver(
            db, thread=thread, message=OutboundMessage(body=prompts.APPLY_LINK_UNREADABLE)
        )
        return TurnResult("clarified", thread, state, sent=[r for r in [row] if r])

    applylink.remember(
        thread,
        url=url,
        posting_id=posting.id,
        status=applylink.STATUS_CONFIRMING,
        steps=1,
    )
    db.flush()
    row = _deliver(
        db,
        thread=thread,
        message=OutboundMessage(
            # verbatim: the employer's name and the role title came off their
            # own page, and a rewrite that changes either makes the confirm a
            # lie about what we are agreeing to.
            body=prompts.APPLY_LINK_CONFIRM.format(
                title=posting.title, company=posting.company
            ),
            asked_field=applylink.ASK_CONFIRM,
            verbatim=True,
        ),
    )
    return TurnResult("asked", thread, state, sent=[r for r in [row] if r])


def _advance_apply_link(
    db: Session, *, thread: IntakeThread, last: IntakeMessage, state, intent
):
    """Move a pending request one step, or ``None`` if it has nothing to do.

    ``None`` matters: it hands the turn back to the intent router rather than
    swallowing it, so a thread with a parked request still answers "what's my
    status".
    """
    from .conversation import TurnResult, _deliver

    request = applylink.pending(thread)
    if not request or not request.posting_id:
        return None
    posting = db.get(JobPosting, request.posting_id)
    if posting is None:
        applylink.clear(thread)
        return None

    if intent is not None and intent.kind == intents.INTENT_DECLINE:
        # A refusal is about the request, not about roles in general -- it is
        # the one intent that has to reach here rather than being treated as
        # navigation. Before the confirm it means "not that role"; after it,
        # "not right now", which parks rather than discards: they already said
        # they wanted it, and a resume arriving next week should still work.
        if request.status == applylink.STATUS_CONFIRMING:
            applylink.clear(thread)
        else:
            applylink.park(thread)
        db.flush()
        # None, so the ordinary decline acknowledgement goes out. Two messages
        # for one "no" is exactly the noise the budget exists to avoid.
        return None

    if request.status == applylink.STATUS_CONFIRMING:
        # Anything that is not a refusal, on the turn after "want me to apply
        # to that one", is a yes. `intents._NEGATIVE` is a closed set for
        # exactly this reason: "yeah go for it" and "sure" and "please do"
        # cannot all be enumerated, but the refusals can.
        applylink.remember(thread, status=applylink.STATUS_GATHERING)
        request = applylink.pending(thread)

    candidate_id = thread.candidate_id or identity.link_candidate(db, thread=thread)
    candidate = db.get(Candidate, candidate_id) if candidate_id else None
    gaps = applylink.missing(db, thread=thread, candidate=candidate)

    if not gaps and candidate is not None:
        body, action = applylink.submit_request(
            db,
            thread=thread,
            posting=posting,
            candidate_id=candidate.id,
            consenting_message_id=last.id if last is not None else None,
            consent_verbatim=(last.body or "") if last is not None else "",
        )
        applylink.clear(thread)
        row = _deliver(db, thread=thread, message=OutboundMessage(body=body, verbatim=True))
        return TurnResult(action, thread, state, sent=[r for r in [row] if r])

    return _ask_for_gap(db, thread=thread, state=state, request=request, gaps=gaps)


def _ask_for_gap(db: Session, *, thread: IntakeThread, state, request, gaps):
    """One item, one message -- or park it and stop asking.

    The budget is what stops the nagging. One message too many gets a number
    blocked and every candidate after that silently receives nothing, so a
    fourth ask is a worse outcome than a request that waits.
    """
    from .conversation import TurnResult, _deliver
    from .uploads import mint_upload_link

    need = next((item for item in gaps if not applylink.asked_too_often(thread, item)), "")
    if not need or request.steps >= applylink.MAX_STEPS:
        already_parked = request.status == applylink.STATUS_PARKED
        applylink.park(thread)
        db.flush()
        if already_parked:
            # Told them once already. Saying it again is the nagging the budget
            # exists to prevent, so the turn goes back to the intent router.
            return None
        row = _deliver(
            db,
            thread=thread,
            # Not verbatim: `lowercase_outbound` already holds URLs out of the
            # voice transform, so the signed link survives and the prose keeps
            # the same voice as every other message on this thread.
            message=OutboundMessage(
                body=prompts.APPLY_LINK_PARKED.format(upload_link=mint_upload_link(thread))
            ),
        )
        return TurnResult("parked", thread, state, sent=[r for r in [row] if r])

    bodies = {
        applylink.NEED_EMAIL: prompts.APPLY_LINK_NEED_EMAIL,
        applylink.NEED_NAME: prompts.APPLY_LINK_NEED_NAME,
        applylink.NEED_RESUME: prompts.APPLY_LINK_NEED_RESUME,
    }
    body = bodies[need]
    if need == applylink.NEED_RESUME:
        body = body.format(upload_link=mint_upload_link(thread))
    applylink.remember(
        thread,
        steps=request.steps + 1,
        status=(
            applylink.STATUS_WAITING_RESUME
            if need == applylink.NEED_RESUME
            else applylink.STATUS_GATHERING
        ),
    )
    db.flush()
    row = _deliver(
        db,
        thread=thread,
        message=OutboundMessage(body=body, asked_field=applylink.ask_field_for(need)),
    )
    return TurnResult("asked", thread, state, sent=[r for r in [row] if r])


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


def _drop_dead_postings(db: Session, pairs: list) -> list:
    """Check the handful about to be sent, and close the ones that are gone.

    Freshness elsewhere means "recently seen", which is not the same as still
    existing -- a live thread was sent a Rippling role whose page had already
    been taken down. This is the last moment before a person clicks it.

    Closing the posting matters as much as skipping it: `eligible_matches`
    only reads open postings, so a dead role removed here does not come back
    on the next request.
    """
    from ..jobmatch.liveness import is_gone

    alive = []
    for match, posting in pairs:
        url = (posting.source_url or "").strip()
        if url and is_gone(url):
            posting.status = "closed"
            db.flush()
            logger.info("closed dead posting %s before proposing it", posting.id)
            continue
        alive.append((match, posting))
    return alive


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

    pairs = _drop_dead_postings(db, pairs)
    if not pairs:
        if quiet:
            return None
        row = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(
                body=(
                    "the roles i had lined up have already been taken down. "
                    "i'll keep looking and text you when something lands."
                )
            ),
            origin=origin,
        )
        return TurnResult(ACTION_ANSWERED, thread, state, sent=[r for r in [row] if r])

    cards = [jobcard(posting, match) for match, posting in pairs]
    link = mint_portal_link(
        db, org_id=thread.org_id, candidate_id=candidate_id, channel=thread.channel
    )

    # One message per role, not one message listing every role. Three roles in
    # a single bubble is a wall of text somebody scrolls past; sent separately
    # each one gets its own bubble, which is how a phone renders a list of
    # things and what a person can react to individually. A thumbs-up on a
    # bubble is only meaningful if the bubble is one job.
    rows = []
    for index, (card, (_match, posting)) in enumerate(zip(cards, pairs, strict=True), start=1):
        candidate_facing = {k: v for k, v in card.items() if k != "recruiter_only"}
        # The image rides along; it never replaces the text. A card that fails
        # to render, upload or download still has to leave a readable role in
        # the bubble, and on SMS there is deliberately no image at all --
        # `card_media` returns empty for both and the body is byte-identical.
        media = card_media(db, thread=thread, posting=posting, card=candidate_facing)
        # verbatim: the card quotes employers and role titles out of a posting.
        # Those are somebody else's proper nouns, not our lowercase voice.
        rows.append(
            _deliver(
                db,
                thread=thread,
                message=OutboundMessage(
                    body=render_card(
                        candidate_facing,
                        index=index,
                        total=len(cards),
                    ),
                    verbatim=True,
                    media=media,
                ),
                origin=origin,
            )
        )
    row = rows[0] if rows else None
    tail = _deliver(
        db,
        thread=thread,
        message=OutboundMessage(
            body=(
                "tap the ones you want me to apply to — or reply with a number "
                f"for detail. all of them: {link}"
            )
        ),
        origin=origin,
    )
    rows.append(tail)

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
            # The message that carried THIS card. A reaction arrives with a
            # message_id, so a thumbs-up can only be resolved to a job if each
            # proposal remembers its own bubble.
            message_id=(
                rows[len(frame)].id
                if len(frame) < len(rows) and rows[len(frame)] is not None
                else None
            ),
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
    return TurnResult(ACTION_PRESENTED, thread, state, sent=[r for r in rows if r])


