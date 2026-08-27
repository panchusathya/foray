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
from ..presentation.jobcard import clean_title, jobcard, render_card, role_line
from . import apply_link, identity, intents, prompts
from . import cards as cards_media
from .channels import OutboundMessage
from .threads import was_handed_off

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
        canned = body
        if not verbatim:
            from .conversation import humanize

            body = humanize(thread, says=body, last_inbound=last.body or "")
        row = _deliver(
            db, thread=thread, message=OutboundMessage(body=body, verbatim=verbatim)
        )
        if row is not None and not verbatim:
            # Which template this bubble came from, for `_fresh`: the stored
            # body is a model rewrite, so comparing surfaces never matches.
            row.raw = {**(row.raw or {}), "canned": canned}
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
        #
        # Deliberately ahead of the pending-application branch below, and left
        # alone. A bare "yes" against "any visa sponsorship needed, or are you
        # authorised to work there?" is genuinely ambiguous -- it could be
        # either half -- and the extractor cannot tell them apart either. So a
        # yes stays a yes to roles, and an answer that matters arrives in words.
        return present_matches(db, thread=thread, state=state)

    # Everything below is a reply with no command in it. If an application has
    # asked this candidate a question and is still waiting, that is what they
    # are replying to -- and the generic branches were the bug: an answer to
    # "what should we put for comp expectations?" got "noted, thanks. anything
    # else you want me looking for?", which named no role and invited a fresh
    # job search, and a following "no" then read as declining one.
    answered = _absorb_application_answer(db, thread=thread, last=last, state=state)
    if answered is not None:
        return answered

    if intent.kind == intents.INTENT_ACK:
        return _acknowledge(db, thread=thread, last=last, state=state, intent=intent)
    if intent.kind == intents.INTENT_DECLINE:
        return _say(_fresh(db, thread, prompts.MANAGER_ACK_DECLINE, prompts.MANAGER_STANDING_BY))
    if intent.kind == intents.INTENT_PROFILE_ANSWER:
        return _say(_fresh(db, thread, prompts.MANAGER_NOTED, prompts.MANAGER_NOTED_AGAIN))
    return _say(_fresh(db, thread, prompts.MANAGER_FALLBACK, prompts.MANAGER_FALLBACK_AGAIN))


def _absorb_application_answer(db: Session, *, thread: IntakeThread, last: IntakeMessage, state):
    """Fold a reply into the application that asked, or return None.

    ``None`` means no application was waiting, and the caller carries on with
    the ordinary intent branches -- this must not become a catch-all that
    swallows conversation whenever an old application has a blank on it, which
    is why ``awaiting_text_answers`` requires the row to have actually been
    asked about.

    The preference is already updated by this point: ``_managing_turn`` runs
    ``_reread`` and ``_capture`` before any of this, so the extractor has seen
    the message. What was missing was the second half -- folding that into the
    application's own ``form_answers``, which only ever happened when the
    candidate opened the portal.
    """
    from ..applications import followups
    from .conversation import TurnResult, _deliver

    if not thread.candidate_id:
        return None
    application = followups.awaiting_text_answers(
        db, org_id=thread.org_id, candidate_id=thread.candidate_id
    )
    if application is None:
        return None

    outcome = followups.absorb_text_answers(
        db,
        application,
        reply_body=last.body or "",
        asked_key=thread.last_field_asked or "",
    )
    posting = db.get(JobPosting, application.job_posting_id)
    body = followups.answer_acknowledgement(application, posting, outcome)

    # Answering is what makes an attempt possible, so this is where one is
    # retried. The first attempt ran at package time, before these answers
    # existed: a required blank refuses the run before the browser opens, so
    # without this the candidate answers the question and nothing whatsoever
    # happens. `auto_submit` reports rather than raises, returns any attempt
    # already in flight instead of queueing a second, and most postings have no
    # adapter at all -- so a hand-off stays a hand-off.
    submission = ""
    if not outcome["missing"]:
        from ..applications.submission import auto_submit

        _, submission = auto_submit(db, application)
        logger.info(
            "answers completed application %s; submission: %s",
            application.id,
            submission,
        )
        if "first application" in submission and outcome.get("next_question") is None:
            # The gate refused the send the acknowledgement was about to
            # promise. "i'll take it from here" followed by silence is the
            # agent lying — a live thread answered "just fire the app looks
            # good" and nothing fired. Say the one step that actually does.
            from ..presentation.jobcard import role_line

            link = mint_portal_link(
                db,
                org_id=thread.org_id,
                candidate_id=thread.candidate_id,
                path=f"/applications/{application.id}",
                channel=thread.channel,
            )
            where = (
                role_line(posting.title, posting.company)
                if posting is not None
                else "your application"
            )
            body = (
                f"that's everything for {where}. one last step since it's your "
                f"first with me: give it a look and tap approve, and i'll send "
                f"it — {link}"
            )

    # verbatim: names the role and the employer out of the posting. When the
    # acknowledgement carries the form's next question, the outbound is tagged
    # with its key so the reply lands on that row -- identity-question
    # mechanics, reused.
    next_question = outcome.get("next_question")
    asked_field = str(next_question.get("key")) if next_question else ""
    row = _deliver(
        db,
        thread=thread,
        message=OutboundMessage(body=body, verbatim=True, asked_field=asked_field),
    )
    if row is not None and asked_field:
        followups.mark_text_asked(db, application, asked_field)
    result = TurnResult(ACTION_ANSWERED, thread, state, sent=[r for r in [row] if r])
    logger.info(
        "application %s absorbed answers %s (still missing %s)",
        application.id,
        outcome["filled"],
        outcome["missing"],
    )
    return result


def apply_to_proposal(db: Session, *, thread: IntakeThread, proposal_id: str) -> dict:
    """Apply to one named proposal. The entry point a tapback uses.

    ``_apply`` resolves an ordinal out of what was last presented, because a
    typed "apply to 2" is all we have to go on there. A reaction names its
    message directly, so the proposal is already known and re-deriving it from
    a position would only introduce a way to get it wrong -- the frame can
    have moved on by the time somebody scrolls back and taps an older card.

    The rails live here rather than at the caller because this function is the
    entry point below ``run_turn``: the worker's tapback branch and the legacy
    poll path both return before a turn is ever run, so a candidate who texted
    STOP could scroll up two days later, thumbs-up last week's job card, and
    get an Application row plus an "on it" text on a number that is on the
    do-not-contact list. ``_deliver`` only ever consults
    ``thread.consent_messaging``, and ``handle_stop`` does not clear it.
    """
    refused = _rails_for_out_of_band_apply(db, thread=thread)
    if refused is not None:
        return {**refused, "proposal_id": proposal_id}

    proposal = db.get(JobProposal, proposal_id)
    if proposal is None:
        return {"skipped": "proposal not found", "proposal_id": proposal_id}

    presented = list(thread.last_presented or [])
    index = next(
        (i for i, e in enumerate(presented, start=1) if e.get("proposal_id") == proposal_id),
        0,
    )
    if index == 0:
        # Reacted to a card from an older batch. The proposal is still the
        # truth; rebuild a one-entry frame so `_apply` has something to read.
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

    last = None
    if proposal.message_id:
        last = db.get(IntakeMessage, proposal.message_id)
    if last is None:
        last = _last_inbound(db, thread)
    if last is None:
        return {"skipped": "no consent message", "proposal_id": proposal_id}
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


def _rails_for_out_of_band_apply(db: Session, *, thread: IntakeThread) -> dict | None:
    """``run_turn``'s safety gates, for an apply that never reaches ``run_turn``.

    Same order and same outcomes as ``conversation.run_turn``: an already
    stopped thread, then suppression (which closes the thread, exactly as the
    turn does, so the opt-out is durable), then quiet hours. The quiet-hours
    refusal is a deferral rather than a drop -- the worker re-queues on
    ``defer_until``, so the application still happens, it just does not text
    somebody at 3am to tell them about it.
    """
    from . import cadence
    from .channels import get_channel
    from .suppression import is_blocked
    from .threads import STATUS_STOPPED, close_thread

    if thread.status == STATUS_STOPPED:
        return {"skipped": "thread stopped", "thread_id": thread.id}
    if is_blocked(db, thread=thread):
        close_thread(db, thread=thread, status=STATUS_STOPPED, reason="do-not-contact")
        return {"skipped": "do-not-contact", "thread_id": thread.id}

    # Consent-gated channels only, and only when consent is on file -- the same
    # carve-out ``run_turn`` makes. A thread with no consent is refused by
    # ``_deliver`` whatever the clock says, so deferring it would hide that
    # refusal behind a timer.
    if get_channel(thread.channel).requires_consent() and thread.consent_messaging:
        decision = cadence.may_send_now(db, thread=thread)
        if not decision.allowed:
            return {
                "defer": True,
                "defer_until": decision.resume_at,
                "reason": decision.reason,
                "thread_id": thread.id,
            }
    return None


def _last_inbound(db: Session, thread: IntakeThread) -> IntakeMessage:
    """Fallback consent trail when the proposal has no card message.

    A tapback IS the consent, and it is not an IntakeMessage — so the card
    it reacted to (`proposal.message_id`) is the right row. This helper is
    only the fallback for a typed "apply to 2" that arrived without a
    named proposal, or a card we failed to backfill.
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

    body = prompts.APPLY_ACK.format(
        title=clean_title(posting.title, posting.company), company=posting.company
    )
    action = ACTION_APPLIED
    application = None
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
    except DuplicateApplicationError:
        # Already pursuing it, so the proposal really was accepted -- just not
        # by this tap.
        _accept_proposal(proposal, last)
        body = (
            f"You're already in flight for {role_line(posting.title, posting.company)} — "
            'ask me "status" any time.'
        )
        action = ACTION_ANSWERED
    except ApplicationError as exc:
        # Deliberately NOT accepted. Marking the proposal accepted ahead of the
        # attempt left a refused application looking agreed-and-handled: the
        # posting could never be re-presented, nothing was queued, and there is
        # no retry path -- so "I'll check back with you" was a promise with
        # nothing behind it. Left at "proposed", the card is still live and
        # another tap (or another ask) retries once the budget frees up.
        logger.info("apply parked on thread %s: %s", thread.id, exc)
        body = prompts.APPLY_PARKED.format(
            title=clean_title(posting.title, posting.company), company=posting.company
        )
        action = ACTION_ANSWERED
    else:
        _accept_proposal(proposal, last)

    if application is not None:
        try:
            request_package(db, application)
        except ApplicationError as exc:
            # The application exists and the yes is recorded; only the build
            # did not start. Saying "at your limit" here would name a rule that
            # did not fire, so the ack stands and a recruiter picks the package
            # up from the board.
            logger.warning(
                "package request failed for application %s: %s", application.id, exc
            )

    row = _deliver(db, thread=thread, message=OutboundMessage(body=body))
    return TurnResult(action, thread, state, sent=[r for r in [row] if r])


def _accept_proposal(proposal: JobProposal | None, last: IntakeMessage) -> None:
    """Record the yes, once the application it authorises actually exists."""
    if proposal is not None and proposal.status == "proposed":
        proposal.status = "accepted"
        proposal.responded_at = utcnow()
        proposal.responded_message_id = last.id


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

    # Compared against the template the last bubble was written FROM (stashed
    # on the row by `_say`), not its surface text: with a model key the stored
    # body is a rewrite and never string-equals the constant. Case-insensitive
    # for the keyless path, where the channel voice lowercases the constant.
    for message in reversed(messages_of(db, thread)):
        if message.direction != OUTBOUND:
            continue
        said = str((message.raw or {}).get("canned") or message.body or "").strip().lower()
        return second if said == first.strip().lower() else first
    return first


def _sendable_postings(db: Session, pairs: list, *, limit: int) -> list:
    """Walk best-first and keep the first ``limit`` still worth carding.

    One check per posting, at the last moment before a person acts on it:

    **Still up.** Freshness elsewhere means "recently seen", which is not the
    same as still existing -- a live thread was sent a Rippling role whose page
    had already been taken down. Closing the posting matters as much as
    skipping it: `eligible_matches` only reads open postings, so a dead role
    removed here does not come back on the next request.

    The vendor gate that used to live here is gone on purpose. Hiding a match
    because its board has no adapter turned every unsupported-board book into
    "i'm not going to show you roles i can't act on", repeated verbatim
    forever. A match is shown; applying is attempted with whatever tier exists
    (handwritten adapter, keyed API, derived form spec), and a posting nothing
    automated can send becomes a ready-to-review package with the direct link
    in the portal -- an outcome, not a refusal. Derivation is still queued
    here for postings with no spec yet, so the answer is on the row by the
    time somebody taps apply.
    """
    from ..applications import formspec
    from ..applications.ats import detect as ats_detect
    from ..jobmatch.liveness import is_gone

    kept: list = []
    for match, posting in pairs:
        url = (posting.source_url or "").strip()
        if url and is_gone(url):
            posting.status = "closed"
            db.flush()
            logger.info("closed dead posting %s before proposing it", posting.id)
            continue
        # Resolution with ``stamp_on_miss=False`` so an aggregator 403ing our
        # IP today does not cache "unrecognised" forever. A board with no spec
        # yet gets one derivation queued (a read-only page view) -- only here,
        # on a bar-passing match somebody is about to be shown: deriving the
        # whole book would be a page view per posting nobody ever sees.
        ats_detect.resolve_target(db, posting, stamp_on_miss=False)
        formspec.enqueue_derivation(db, posting)
        kept.append((match, posting))
        if len(kept) >= limit:
            break
    return kept


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

    # Over-fetched: the liveness filter below drops dead pages, and a filter
    # over exactly three would let one taken-down role shrink the batch rather
    # than yield to the live one ranked under it.
    pairs = eligible_matches(
        db,
        org_id=thread.org_id,
        candidate_id=candidate_id,
        limit=MAX_CARDS_PER_MESSAGE * 3,
    )
    if pairs:
        pairs = _sendable_postings(db, pairs, limit=MAX_CARDS_PER_MESSAGE)
    if not pairs:
        if quiet:
            return None
        # Asked directly and holding nothing (or holding only roles whose
        # pages are gone): go and look before answering. Reading the book we
        # happen to have is the whole of what "find me roles" used to do, so a
        # candidate whose spec had never been searched -- or whose spec was
        # searched once, at handover, before any of it was parsed -- got
        # "nothing clears the bar" from a search that had never run. Capped at
        # one billed sweep per candidate per day.
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

    # The budget check runs AFTER the liveness filter: "I'll send them
    # tomorrow" must be a promise about roles that still exist, and the daily
    # cap must not be spent on postings about to be dropped as dead.
    decision = limits.may_propose(
        db,
        org_id=thread.org_id,
        candidate_id=candidate_id,
        count=len(pairs),
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

    # One message per role, not one message listing every role. Three roles in
    # a single bubble is a wall of text somebody scrolls past; sent separately
    # each one gets its own bubble, which is how a phone renders a list of
    # things and what a person can react to individually. A thumbs-up on a
    # bubble is only meaningful if the bubble is one job.
    #
    # Proposal first so the vCard URL can include its id; then send; then
    # backfill message_id so a tapback still resolves to this card.
    #
    # The ordinal is `len(frame) + 1`, not the loop index: it is what the
    # candidate will see on this bubble, and it has to be the same number
    # `_apply` resolves against `last_presented`. Cards are delivered one at a
    # time, so by the time this one is rendered every earlier failure is
    # already known.
    rows = []
    frame = []
    now = utcnow()
    for (match, posting), card in zip(pairs, cards, strict=True):
        index = len(frame) + 1
        proposal = JobProposal(
            org_id=thread.org_id,
            candidate_id=candidate_id,
            job_posting_id=posting.id,
            job_match_id=match.id,
            channel=thread.channel,
            thread_id=thread.id,
            status="proposed",
            reasons=card["reasons"],
        )
        db.add(proposal)
        db.flush()

        # The image *is* the card wherever one actually exists; the text card
        # is the message everywhere else. This is conditioned on the media
        # part being in hand -- rendered, uploaded, id returned -- not on the
        # protocol, which is what keeps the vCard's failure mode dead: that
        # was a media-only bubble whose attachment could silently fail to
        # fetch, leaving a role with no detail and no way to act. Here a
        # failed render or upload returns no media and the words go instead,
        # so every bubble carries the role one way or the other. The ordinal
        # ("2 of 3") is baked into the image for the same reason the body
        # carried it: "apply to 2" has to point at something visible.
        shown = {k: v for k, v in card.items() if k != "recruiter_only"}
        media = cards_media.card_media(
            db, thread=thread, posting=posting, card=shown, index=index, total=len(cards)
        )
        message = OutboundMessage(
            body="" if media else render_card(shown, index=index, total=len(cards)),
            media=media,
            verbatim=True,
        )
        row = _deliver(db, thread=thread, message=message, origin=origin)
        if row is None or not was_handed_off(row):
            # A card nobody received must not bury the posting. `JobProposal`
            # is unique per (candidate, posting) and `eligible_matches` treats
            # any row as answered, so a proposal left behind by a carrier 500
            # means "Nothing new clears the bar right now" for that role
            # forever, and a later "apply to 1" resolving to a card the
            # candidate never saw. The failed message row is the evidence the
            # attempt happened; the proposal is not.
            logger.warning(
                "job card for posting %s did not reach thread %s; dropping the proposal",
                posting.id,
                thread.id,
            )
            db.delete(proposal)
            db.flush()
            continue
        proposal.message_id = row.id
        rows.append(row)
        frame.append(
            {
                "proposal_id": proposal.id,
                "posting_id": posting.id,
                "company": posting.company,
                "title": posting.title,
            }
        )

    if not frame:
        # Every card failed. `last_presented` is left exactly as it was: an
        # empty frame throws away whatever the candidate is still looking at,
        # and a frame of undelivered cards is how "apply to 1" applies to a
        # role nobody was shown.
        if quiet:
            return None
        row = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(
                body=(
                    "i've got roles for you but they didn't send just now — "
                    "i'll try again shortly."
                )
            ),
            origin=origin,
        )
        return TurnResult(ACTION_ANSWERED, thread, state, sent=[r for r in [row] if r])

    tail = _deliver(
        db,
        thread=thread,
        message=OutboundMessage(body=prompts.PRESENT_TAIL.format(link=link)),
        origin=origin,
    )
    rows.append(tail)
    thread.last_presented = frame
    thread.presented_at = now
    db.flush()
    return TurnResult(ACTION_PRESENTED, thread, state, sent=[r for r in rows if r])


def _acknowledge(db: Session, *, thread: IntakeThread, last: IntakeMessage, state, intent):
    """A pure thanks / great / haha: tapback, and sometimes a warm line.

    like/laugh stay silent when the reaction landed. Gratitude (love) always
    gets the canned line — that is the deterministic "sometimes more
    conversational" subset. If a reaction is impossible (web, no native id,
    no consent) there is never dead air.
    """
    from .conversation import TurnResult, _deliver, react_to_message

    reacted = False
    if intent.reaction:
        reacted = react_to_message(
            db, thread=thread, message=last, reaction=intent.reaction
        )
    want_line = intent.reaction == "love" or not reacted or not intent.reaction
    sent: list = []
    if want_line:
        row = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(body=prompts.MANAGER_ACK_WARM, verbatim=True),
        )
        sent = [r for r in [row] if r]
    return TurnResult("acknowledged", thread, state, sent=sent)
