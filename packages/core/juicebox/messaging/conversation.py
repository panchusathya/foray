"""One turn of an intake thread: read what arrived, decide what to say.

This is the whole messaging-specific design, and it is small because the
judgement lives elsewhere. ``intake.schema`` decides what is still owed;
``intake.extract`` decides what the conversation said. What is left here is
*cadence* -- the thing voice does not have to think about.

**The gate has a different job here.** On a call, ``required_complete`` decides
when the agent may hang up. On a thread there is nothing to hang up: it decides
whether to send another message at all. The cost of getting it wrong is also
different. A call that runs long is disliked; a thread that sends one message
too many gets the number blocked, and every candidate after that one silently
receives nothing.

Four rules follow from that, and all four are enforced in code rather than
asked for in a prompt:

1. **One question per message.** ``next_questions(record, limit=1)`` in effect,
   and ``prompts.valid_question`` rejects a composed message that smuggles in a
   second.
2. **Never two outbound in a row.** An outbound message requires an inbound one
   after the last thing we sent. The only exceptions are the opening greeting
   and the single opt-out acknowledgement.
3. **A field is asked at most twice.** Copied from the voice plan's "two
   follow-ups per topic max". Past that the field is left missing and flagged
   for review -- it is *not* marked declined, because nobody declined it, and
   recording a refusal that never happened would be inventing an answer.
4. **Enrichment is budgeted.** A call already has someone on the line, so all
   fourteen enrichment fields are nearly free. A text costs a notification
   each, so the thread asks a handful and stops.

Re-extraction runs over the whole transcript every turn rather than over the
new message alone. It costs more, and it is what makes "actually, make that
260" work: the extractor reads the correction in the context that gives it
meaning. ``records.merge_record`` then stops a quieter later pass from undoing
an earlier answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..enrich.linkedin_scrape import parse_years_experience
from ..intake.extract import EXTRACTOR_HEURISTIC, ExtractionResult, extract_intake
from ..intake.schema import (
    FIELDS,
    TIER_ENRICHMENT,
    TIER_REQUIRED,
    Coverage,
    coverage,
    next_questions,
)
from ..jobs import KIND_LINKEDIN_SCRAPE, enqueue
from ..llm.client import get_llm
from ..models import IntakeMessage, IntakeThread, Org
from . import cadence, identity, linkedin_flow, prompts
from .channels import ChannelError, DeliveryResult, OutboundMessage, get_channel
from .identity import CONTACT_LINKEDIN
from .linkedin_flow import (
    BATCH_CONFIRM,
    BATCH_GAP_FILL,
    PHASE_SCRAPING,
    apply_years_to_record,
    confirm_message,
    flow_meta,
    gap_fill_message,
    intro_message,
    is_linkedin_short,
    scrape_failed_message,
    scraped_profile,
    short_coverage,
    update_flow_meta,
    wants_years_question,
)
from .records import merge_record, persist_preference
from .suppression import handle_stop, is_blocked, looks_like_stop
from .threads import (
    INBOUND,
    OUTBOUND,
    STATUS_COMPLETE,
    STATUS_STOPPED,
    close_thread,
    messages_of,
    record_outbound,
    transcript_of,
    with_send_key,
)

logger = logging.getLogger(__name__)

# Two attempts at any one field, matching the voice plan's per-topic cap.
MAX_ASKS_PER_FIELD = 2
# How many enrichment questions a thread may spend before wrapping up. A call
# asks all fourteen because the line is already open; here each one is another
# notification, and the gate has already closed by the time we get to them.
MAX_ENRICHMENT_QUESTIONS = 6

ACTION_GREETED = "greeted"
ACTION_ASKED = "asked"
ACTION_COMPLETED = "completed"
ACTION_STOPPED = "stopped"
ACTION_BLOCKED = "blocked"
ACTION_HELD = "held"
# Quiet hours: the message may not go now but should go later. Carries
# ``resume_at`` so the worker can re-enqueue the turn for when the recipient's
# send window opens -- deferral reuses Job.run_at, there is no scheduler.
ACTION_DEFERRED = "deferred"

_ENRICHMENT_KEYS = frozenset(f.key for f in FIELDS if f.tier == TIER_ENRICHMENT)


@dataclass(frozen=True)
class TurnResult:
    """What the turn did, in terms a caller can act on.

    ``action`` is deliberately explicit rather than inferred from ``sent``:
    "we sent nothing because they opted out" and "we sent nothing because it is
    their turn to speak" are the same empty list and very different situations.
    """

    action: str
    thread: IntakeThread
    coverage: Coverage
    asked_field: str = ""
    sent: list[IntakeMessage] = field(default_factory=list)
    extractor: str = ""
    # Set only with ACTION_DEFERRED: the UTC instant the send window opens.
    resume_at: Any = None


def run_turn(db: Session, *, thread: IntakeThread) -> TurnResult:
    """Advance a thread by at most one outbound message."""
    history = messages_of(db, thread)
    last = history[-1] if history else None

    # Opted out, suppressed, or already finished -- decided before anything is
    # composed, so a blocked thread costs no model call.
    if thread.status == STATUS_STOPPED:
        return TurnResult(ACTION_BLOCKED, thread, _coverage_for(thread))
    if is_blocked(db, thread=thread):
        close_thread(db, thread=thread, status=STATUS_STOPPED, reason="do-not-contact")
        return TurnResult(ACTION_BLOCKED, thread, _coverage_for(thread))
    if thread.status == STATUS_COMPLETE:
        return TurnResult(ACTION_COMPLETED, thread, _coverage_for(thread))

    if last is not None and last.direction == INBOUND and looks_like_stop(last.body):
        handle_stop(db, thread=thread)
        # The single confirmation a carrier expects, and the last thing this
        # thread will ever send. Deliberately not routed through _send, which
        # refuses to send to a stopped thread.
        sent = _deliver(
            db, thread=thread, message=OutboundMessage(body=prompts.STOP_ACKNOWLEDGEMENT)
        )
        return TurnResult(
            ACTION_STOPPED, thread, _coverage_for(thread), sent=[s for s in [sent] if s]
        )

    # Rule 2, hoisted above the flow branches so it covers all three. Their
    # turn: holding is the correct outcome, not a failure, and it is checked
    # before quiet hours because deferring a turn with nothing to send would
    # re-queue work for a message that was never going to exist.
    if last is not None and last.direction == OUTBOUND:
        return TurnResult(ACTION_HELD, thread, _coverage_for(thread))

    # Quiet hours, checked before composing for the same reason is_blocked is,
    # and above the flow branches for a sharper reason: the LinkedIn-short
    # path returns early, so a gate placed after it would let that flow text
    # somebody at 3am while every other flow waited. Only channels that text a
    # phone are gated -- the web widget's delivery is the HTTP response to
    # somebody sitting in front of it. In practice the reply window passes
    # almost every live turn; this bites when a turn is retried hours later
    # off the queue (a slow scrape, a backed-up worker), which is exactly when
    # a 2am send would otherwise slip out.
    # ``and thread.consent_messaging``: quiet hours protect somebody we are
    # allowed to text. A thread with no consent is refused by ``_deliver``
    # whatever the clock says, so deferring it would hide that refusal behind
    # a timer and re-queue a send that can never succeed. Consent is a
    # property of the thread; the window is a property of the moment.
    if get_channel(thread.channel).requires_consent() and thread.consent_messaging:
        decision = cadence.may_send_now(db, thread=thread)
        if not decision.allowed:
            return TurnResult(
                ACTION_DEFERRED,
                thread,
                _coverage_for(thread),
                resume_at=decision.resume_at,
            )

    # LinkedIn-first onboarding: intro -> scrape -> confirm -> one batched
    # gap-fill. It owns the whole conversation until it wraps up, at which
    # point it hands the thread to `managing` rather than completing it --
    # hence the mode guard, or a handed-over thread would loop straight back
    # into onboarding on the candidate's next message.
    from .manager import MODE_MANAGING

    if is_linkedin_short(thread) and thread.mode != MODE_MANAGING:
        return _run_linkedin_short(db, thread=thread, last=last)

    # Opening move: nobody has said anything yet.
    if last is None:
        return _greet(db, thread=thread)

    # The candidate-agent modes take over here, AFTER every safety rail above:
    # STOP, suppression, hold, and quiet hours are shared, and only what one
    # message says next is mode-specific. mode="intake" threads never reach
    # the manager, byte-for-byte the behaviour they always had.
    if thread.mode and thread.mode != "intake":
        from .manager import run_manager_turn

        return run_manager_turn(db, thread=thread, last=last)

    identity.absorb(
        thread,
        body=last.body,
        asked_field=thread.last_field_asked,
        batched=thread.last_field_asked == BATCH_GAP_FILL,
    )
    _reread(db, thread=thread)

    _capture(db, thread=thread)

    state = _coverage_for(thread)
    contact_field = _next_identity_field(thread)
    if contact_field:
        return _ask(
            db,
            thread=thread,
            state=state,
            field_key=contact_field,
            topic=identity.ASKS[contact_field],
            last_inbound=last.body,
        )

    question = _next_question(thread.record or {}, thread.asked_counts or {})
    if question is None:
        return _wrap_up(db, thread=thread, state=state)

    return _ask(
        db,
        thread=thread,
        state=state,
        field_key=question["field"],
        topic=question["asks"],
        last_inbound=last.body,
    )


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------


def _coverage_for(thread: IntakeThread) -> Coverage:
    if is_linkedin_short(thread):
        return short_coverage(thread.record)
    return coverage(thread.record or {})


def _greet(db: Session, *, thread: IntakeThread) -> TurnResult:
    org = db.get(Org, thread.org_id)
    org_name = (getattr(org, "name", "") or "our team").strip()
    state = _coverage_for(thread)

    greeting = prompts.greeting_for(org=org_name)
    sent = _deliver(db, thread=thread, message=OutboundMessage(body=greeting))

    field_key = _next_identity_field(thread)
    if field_key:
        topic = identity.ASKS[field_key]
    else:
        question = _next_question(thread.record or {}, thread.asked_counts or {})
        if question is None:
            return _wrap_up(db, thread=thread, state=state)
        field_key, topic = question["field"], question["asks"]

    asked = _ask(
        db, thread=thread, state=state, field_key=field_key, topic=topic, last_inbound=""
    )
    return TurnResult(
        ACTION_GREETED,
        thread,
        asked.coverage,
        asked_field=asked.asked_field,
        sent=[m for m in [sent, *asked.sent] if m],
    )


def _reread(db: Session, *, thread: IntakeThread) -> None:
    """Re-extract the whole thread and fold the result into the record."""
    transcript = transcript_of(db, thread)
    if not transcript.strip():
        return
    result = _extract(db, thread=thread, transcript=transcript)
    thread.record = merge_record(thread.record, result.record)
    thread.extractor = result.extractor
    thread.schema_version = thread.record.get("schema_version") or thread.schema_version
    thread.low_confidence = list(result.low_confidence)

    state = _coverage_for(thread)
    thread.required_complete = state.required_complete
    thread.fields_missing = [*state.required_missing, *state.enrichment_missing]
    # The extractor's own verdict, widened: an open gate is a reason for a
    # human to look even when every field that *was* answered looks clean.
    thread.review_needed = bool(result.review_needed) or not state.required_complete
    if result.review_reason:
        thread.review_reason = result.review_reason[:400]
    db.flush()


def _extract(db: Session, *, thread: IntakeThread, transcript: str) -> ExtractionResult:
    """Run the extractor over the right view of the conversation.

    The two paths need different input, and getting this wrong fabricates
    preferences rather than merely missing them.

    The **model** reads the whole thread. It has to: an answer of "within a
    month" means nothing without the question above it.

    The **heuristic** reads only the candidate's messages. It is a set of
    regexes over the whole text with no notion of who is speaking, so given our
    own greeting it extracted a job title of "take few details so recruiter can
    match you to right" and a seniority of "mid" -- from a conversation in
    which the candidate had said nothing but their name. Two required fields
    closed on words we wrote ourselves. A preference the candidate never stated
    silently rules out roles they would have taken, and nobody ever sees what
    was filtered away.

    The degrade path is handled explicitly rather than left to
    ``extract_intake``'s internal fallback: a model call that fails would
    otherwise drop to the heuristic still holding the full transcript, which is
    exactly the case this exists to prevent.
    """
    llm = get_llm()
    fast = is_linkedin_short(thread)
    if llm.available:
        result = extract_intake(transcript, known=_known_summary(thread), fast=fast)
        if result.extractor != EXTRACTOR_HEURISTIC:
            return result
        logger.warning("intake extraction degraded to heuristics, re-reading candidate turns")
    return extract_intake(
        transcript_of(db, thread, candidate_only=True), use_llm=False
    )


def _known_summary(thread: IntakeThread) -> str:
    """What the extractor should not treat as newly said.

    Contact details are here because they came from the thread's own columns
    rather than from something the candidate volunteered as a preference.
    """
    lines: list[str] = []
    if thread.contact_name:
        lines.append(f"name: {thread.contact_name}")
    if thread.contact_email:
        lines.append(f"email: {thread.contact_email}")
    if thread.contact_linkedin:
        lines.append(f"linkedin: {thread.contact_linkedin}")
    state = _coverage_for(thread)
    if state.covered:
        lines.append("already answered: " + ", ".join(state.covered))
    if state.declined:
        lines.append("declined: " + ", ".join(state.declined))
    return "\n".join(lines)


def _next_identity_field(thread: IntakeThread) -> str:
    """Which contact detail to ask for, or empty to move on without it.

    The ask cap matters more here than anywhere else. An intake field that
    nobody answers can be declined; a name cannot, so without a cap a person
    who never types anything name-shaped is asked forever. Past the cap the
    conversation carries on and ``_wrap_up`` flags the thread as unattachable.
    """
    counts = thread.asked_counts or {}
    for key in identity.missing_identity(thread):
        if int(counts.get(key, 0)) < MAX_ASKS_PER_FIELD:
            return key
    return ""


def _capture(db: Session, *, thread: IntakeThread, force: bool = False) -> None:
    """Write the preferences out as soon as they are worth reading.

    Not held back until the conversation ends. Once the required gate has
    closed the record is already what a client asks about, and a thread that
    then goes quiet for a week would otherwise keep it where matching cannot
    see it. ``persist_preference`` is an upsert, so later turns keep the row
    current as enrichment answers arrive.

    ``force`` is the wrap-up case, which persists a partial record too --
    the same contract a call that drops early has.
    """
    if not force and not thread.required_complete:
        return
    candidate_id = identity.link_candidate(db, thread=thread)
    if candidate_id:
        persist_preference(db, thread=thread, candidate_id=candidate_id)


def _next_question(record: dict[str, Any], asked_counts: dict[str, Any]) -> dict[str, str] | None:
    """The next field to ask about, or ``None`` when the thread should wrap up.

    Required tier is exhausted before enrichment is touched -- if somebody has
    not told us what they want to be paid, asking which industries appeal is
    noise. A field over the ask cap is skipped, never marked declined: nobody
    refused it, and recording a refusal that did not happen would put an
    invented answer into a record that matching reads.
    """
    counts = {k: int(v or 0) for k, v in (asked_counts or {}).items()}
    state = coverage(record)
    ordered = next_questions(record, limit=len(FIELDS))

    if state.required_missing:
        for question in ordered:
            if question["tier"] != TIER_REQUIRED:
                break
            if counts.get(question["field"], 0) < MAX_ASKS_PER_FIELD:
                return question
        return None

    spent = sum(count for key, count in counts.items() if key in _ENRICHMENT_KEYS)
    if spent >= MAX_ENRICHMENT_QUESTIONS:
        return None
    for question in ordered:
        if question["tier"] != TIER_ENRICHMENT:
            continue
        if counts.get(question["field"], 0) < MAX_ASKS_PER_FIELD:
            return question
    return None


def _ask(
    db: Session,
    *,
    thread: IntakeThread,
    state: Coverage,
    field_key: str,
    topic: str,
    last_inbound: str,
) -> TurnResult:
    """Send one question about ``field_key``.

    ``topic`` is the schema's own line -- what the field is *for*. The wording
    that goes out comes from ``prompts.question_for``, because the schema line
    is a checklist item for a voice agent, not something you can say to
    somebody. The topic still travels to the model so a rewrite cannot drift
    off what the field is asking about.
    """
    asks = prompts.question_for(field_key, topic)
    skip_compose = field_key.startswith("batch.") or field_key == CONTACT_LINKEDIN
    if skip_compose:
        body = asks
    else:
        body = _compose(
            asks=asks, topic=topic, last_inbound=last_inbound, known=_known_summary(thread)
        )
    sent = _deliver(
        db, thread=thread, message=OutboundMessage(body=body, asked_field=field_key)
    )
    return TurnResult(
        ACTION_ASKED,
        thread,
        state,
        asked_field=field_key,
        sent=[s for s in [sent] if s],
        extractor=thread.extractor,
    )


def _compose(*, asks: str, topic: str, last_inbound: str, known: str) -> str:
    """Make the written question sound like a reply, or send it as written.

    The fallback is not a degraded mode. ``prompts.QUESTIONS`` holds a
    complete, plainly worded question for every field, so a deployment with no
    API key runs an intake conversation that reads a little flatter and
    collects exactly the same record.
    """
    llm = get_llm()
    if not llm.available or not last_inbound.strip():
        return asks
    try:
        result = llm.json_fast(
            system=prompts.COMPOSE_SYSTEM,
            prompt=prompts.compose_prompt(
                asks=asks, topic=topic, last_inbound=last_inbound, already_known=known
            ),
            schema=prompts.COMPOSE_SCHEMA,
        )
        candidate = str((result.data or {}).get("message") or "").strip()
    except Exception as exc:  # noqa: BLE001 - degrade, never fail a turn
        logger.warning("intake compose failed, sending the canned question: %s", exc)
        return asks

    if not prompts.valid_question(candidate):
        logger.warning("intake compose rejected, sending the canned question")
        return asks
    return candidate


def _last_inbound_body(db: Session, thread: IntakeThread) -> str:
    """What they most recently said, for the composer to acknowledge."""
    for message in reversed(messages_of(db, thread)):
        if message.direction == INBOUND:
            return message.body or ""
    return ""


def _first_name(thread: IntakeThread) -> str:
    """Their first name, if we actually have one. Never a guess."""
    name = (thread.contact_name or "").strip()
    return name.split()[0] if name else ""


def humanize(
    thread: IntakeThread, *, says: str, last_inbound: str, batched: bool = True
) -> str:
    """Say the same thing, but like a person.

    Every line this flow sends used to be a template, which is exactly why it
    read as a survey: "- what are you targeting on comp (base is fine)?" is a
    form field with a dash in front of it. The content is still decided in
    code -- what gets asked, and when -- and the model only chooses the words,
    so a broken model degrades to the flat version rather than to a broken
    conversation.

    The canned text is returned unchanged when there is no model, nothing to
    react to, or the rewrite fails the structural guard.
    """
    if not says.strip():
        return says
    llm = get_llm()
    if not llm.available or not last_inbound.strip():
        return says
    try:
        result = llm.json_fast(
            system=prompts.COMPOSE_BATCH_SYSTEM,
            prompt=prompts.compose_batch_prompt(
                says=says,
                last_inbound=last_inbound,
                already_known=_known_summary(thread),
                first_name=_first_name(thread),
            ),
            schema=prompts.COMPOSE_SCHEMA,
        )
        written = str((result.data or {}).get("message") or "").strip()
    except Exception as exc:  # noqa: BLE001 - degrade, never fail a turn
        logger.warning("compose failed, sending the canned copy: %s", exc)
        return says
    if not prompts.valid_question(written, batched=batched):
        logger.warning("compose rejected by the guard, sending the canned copy")
        return says
    # The batched guard only checks length, so it cannot see an invented
    # question -- and an invented question gets a real answer that nobody
    # wanted, then sits in the record as though we asked it on purpose.
    # Counting question marks is crude but it is the one structural signal
    # that a rewrite grew a question the code did not ask for.
    if written.count("?") > says.count("?"):
        logger.warning(
            "compose added a question (%d -> %d), sending the canned copy",
            says.count("?"),
            written.count("?"),
        )
        return says
    return written


def _deliver(
    db: Session, *, thread: IntakeThread, message: OutboundMessage, origin: str = "reply"
) -> IntakeMessage | None:
    """Hand a message to the channel and record what happened either way.

    A channel that requires consent and has not been given it is refused here
    rather than in the channel: the refusal has to be recorded on the thread,
    and a channel does not have a session.
    """
    body = message.body if message.verbatim else prompts.lowercase_outbound(message.body)
    lowered = OutboundMessage(
        body=body,
        asked_field=message.asked_field,
        send_key=message.send_key,
        verbatim=message.verbatim,
    )
    channel = get_channel(thread.channel)
    if not channel.is_configured():
        logger.warning(
            "refusing to send on %s thread %s: channel not configured",
            thread.channel,
            thread.id,
        )
        return record_outbound(
            db,
            thread=thread,
            message=lowered,
            result=DeliveryResult(status="failed", error="channel not configured"),
        )
    if channel.requires_consent() and not thread.consent_messaging:
        logger.warning("refusing to send on %s thread %s: no consent", thread.channel, thread.id)
        return record_outbound(
            db,
            thread=thread,
            message=lowered,
            result=DeliveryResult(status="failed", error="no messaging consent on file"),
        )

    outbound = with_send_key(db, thread=thread, message=lowered)
    try:
        result = channel.send(
            to_address=thread.from_address,
            message=outbound,
            provider_thread_id=thread.provider_thread_id,
        )
    except ChannelError as exc:
        logger.warning("intake send failed on thread %s: %s", thread.id, exc.detail)
        result = DeliveryResult(status="failed", error=exc.detail[:400])
    return record_outbound(db, thread=thread, message=outbound, result=result, origin=origin)


def deliver(
    db: Session, *, thread: IntakeThread, message: OutboundMessage
) -> IntakeMessage | None:
    """Public entry for routes that need one outbound without running a full turn."""
    return _deliver(db, thread=thread, message=message)


def _wrap_up(db: Session, *, thread: IntakeThread, state: Coverage) -> TurnResult:
    """Close the conversation and write the preferences.

    The candidate row is created here and nowhere earlier: a public endpoint
    that made one on the first inbound message would let anyone fill the CRM by
    opening a chat and typing once.

    A thread that ends with the gate still open still persists. A partial
    record flagged for review is worth more than nothing, and ``fields_missing``
    says exactly what is absent -- which is the same contract a call that drops
    early has.
    """
    # A thread the candidate agent runs does not end here: onboarding is
    # finished, the relationship is not. It hands over to `managing`, which
    # sends cards, prepares applications and answers "what's my status" for
    # as long as the person wants. Only a plain intake thread completes --
    # run_turn is silent forever on a complete thread, and a recruiting
    # manager's line going dead mid-search is a product failure, not an end
    # state.
    if _hands_over_to_manager(thread):
        return _hand_over_to_manager(db, thread=thread, state=state)

    body = prompts.WRAP_UP_COMPLETE if state.required_complete else prompts.WRAP_UP_PARTIAL
    sent = _deliver(db, thread=thread, message=OutboundMessage(body=body))

    _capture(db, thread=thread, force=True)
    if not thread.candidate_id:
        thread.review_needed = True
        thread.review_reason = identity.UNATTRIBUTED_REASON

    close_thread(db, thread=thread, status=STATUS_COMPLETE)
    return TurnResult(
        ACTION_COMPLETED, thread, state, sent=[s for s in [sent] if s], extractor=thread.extractor
    )


def _hands_over_to_manager(thread: IntakeThread) -> bool:
    """Does this thread continue after onboarding?

    The LinkedIn-short flow always does -- it only runs on a channel where the
    candidate has a standing line to the agent. A web-widget intake does not:
    nobody is waiting on the other end of a careers-page chat.
    """
    from .manager import MODE_INTAKE

    return is_linkedin_short(thread) or (thread.mode or MODE_INTAKE) != MODE_INTAKE


def _hand_over_to_manager(
    db: Session, *, thread: IntakeThread, state: Coverage
) -> TurnResult:
    from .manager import MODE_MANAGING

    _capture(db, thread=thread, force=True)
    identity.link_candidate(db, thread=thread)
    if not thread.candidate_id:
        thread.review_needed = True
        thread.review_reason = identity.UNATTRIBUTED_REASON

    thread.mode = MODE_MANAGING
    db.flush()
    sent = _deliver(
        db,
        thread=thread,
        message=OutboundMessage(
            body=humanize(
                thread,
                says=prompts.MANAGING_READY,
                last_inbound=_last_inbound_body(db, thread),
            )
        ),
    )
    return TurnResult(
        "completed_profile",
        thread,
        state,
        sent=[s for s in [sent] if s],
        extractor=thread.extractor,
    )


# --------------------------------------------------------------------------
# LinkedIn-first short flow
# --------------------------------------------------------------------------


def _run_linkedin_short(
    db: Session, *, thread: IntakeThread, last: IntakeMessage | None
) -> TurnResult:
    """Intro → scrape → confirm → one gap-fill batch → wrap up."""
    history = messages_of(db, thread)
    outbound_sent = any(m.direction == OUTBOUND and m.status == "sent" for m in history)

    if last is not None and last.direction == INBOUND:
        identity.absorb(
            thread,
            body=last.body,
            asked_field=thread.last_field_asked,
            # The gap-fill asks up to three things at once, so the reply is a
            # list. Without this the name is dropped, nothing links a
            # candidate, and every downstream step -- matching, proposals,
            # "find me roles" -- silently has nobody to work with.
            batched=thread.last_field_asked == BATCH_GAP_FILL,
        )

    if last is None or (last.direction == INBOUND and not outbound_sent and not thread.contact_email):
        return _linkedin_intro(db, thread=thread)

    if last is not None and last.direction == OUTBOUND:
        return TurnResult(ACTION_HELD, thread, _coverage_for(thread))

    assert last is not None

    if not thread.contact_linkedin:
        sent = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(
                body=identity.ASKS[CONTACT_LINKEDIN],
                asked_field=CONTACT_LINKEDIN,
            ),
        )
        return TurnResult(
            ACTION_ASKED,
            thread,
            _coverage_for(thread),
            asked_field=CONTACT_LINKEDIN,
            sent=[s for s in [sent] if s],
        )

    if thread.contact_linkedin and not _scrape_settled(thread):
        _enqueue_linkedin_scrape(db, thread)
        return TurnResult(ACTION_HELD, thread, _coverage_for(thread))

    _reread(db, thread=thread)
    _absorb_years(thread, last.body)
    if flow_meta(thread).get("scrape") and not thread.contact_name:
        name = str(scraped_profile(thread).get("full_name") or "").strip()
        if name:
            thread.contact_name = name[:240]

    asked = thread.last_field_asked or ""
    if asked in ("", CONTACT_LINKEDIN, identity.CONTACT_EMAIL):
        return _linkedin_after_scrape(db, thread=thread, last=last)
    if asked == BATCH_CONFIRM:
        return _linkedin_after_confirm(db, thread=thread, last=last)
    return _wrap_up(db, thread=thread, state=_coverage_for(thread))


def _scrape_settled(thread: IntakeThread) -> bool:
    meta = flow_meta(thread)
    return bool(meta.get("scrape") or meta.get("error"))


def _linkedin_intro(db: Session, *, thread: IntakeThread) -> TurnResult:
    org = db.get(Org, thread.org_id)
    org_name = (getattr(org, "name", "") or "our team").strip()
    body = intro_message(recruiter=prompts.recruiter_name(), org=org_name)
    sent = _deliver(
        db, thread=thread, message=OutboundMessage(body=body, asked_field=identity.CONTACT_EMAIL)
    )
    update_flow_meta(thread, phase="intro")
    return TurnResult(
        ACTION_GREETED,
        thread,
        _coverage_for(thread),
        asked_field=identity.CONTACT_EMAIL,
        sent=[s for s in [sent] if s],
    )


def _enqueue_linkedin_scrape(db: Session, thread: IntakeThread) -> None:
    meta = flow_meta(thread)
    if meta.get("phase") == PHASE_SCRAPING and not _scrape_settled(thread):
        # Already queued; a second inbound while we wait must not spend twice.
        return
    url = thread.contact_linkedin
    update_flow_meta(thread, phase=PHASE_SCRAPING)
    enqueue(
        db,
        org_id=thread.org_id,
        kind=KIND_LINKEDIN_SCRAPE,
        idempotency_key=f"{KIND_LINKEDIN_SCRAPE}:{thread.id}:{url}",
        payload={"thread_id": thread.id, "linkedin_url": url},
    )


def _linkedin_after_scrape(
    db: Session, *, thread: IntakeThread, last: IntakeMessage
) -> TurnResult:
    meta = flow_meta(thread)
    if meta.get("scrape"):
        body = confirm_message(scraped_profile(thread))
        sent = _deliver(
            db, thread=thread, message=OutboundMessage(body=body, asked_field=BATCH_CONFIRM)
        )
        update_flow_meta(thread, phase="confirm")
        return TurnResult(
            ACTION_ASKED,
            thread,
            _coverage_for(thread),
            asked_field=BATCH_CONFIRM,
            sent=[s for s in [sent] if s],
            extractor=thread.extractor,
        )
    # Scrape failed — skip confirm and go straight to the leftover questions.
    preface = scrape_failed_message()
    return _linkedin_gap_fill(db, thread=thread, last=last, preface=preface)


def _linkedin_after_confirm(
    db: Session, *, thread: IntakeThread, last: IntakeMessage
) -> TurnResult:
    missing = linkedin_flow.short_missing(thread)
    # No name gate: the name comes from the profile, and holding onboarding
    # open for one would mean asking for it, which is the survey question we
    # just removed.
    if not missing:
        return _wrap_up(db, thread=thread, state=_coverage_for(thread))
    return _linkedin_gap_fill(db, thread=thread, last=last)


def _linkedin_gap_fill(
    db: Session,
    *,
    thread: IntakeThread,
    last: IntakeMessage,
    preface: str = "",
) -> TurnResult:
    body, keys = gap_fill_message(thread, preface=preface)
    if not body:
        return _wrap_up(db, thread=thread, state=_coverage_for(thread))
    body = humanize(thread, says=body, last_inbound=(last.body if last else ""))
    sent = _deliver(
        db, thread=thread, message=OutboundMessage(body=body, asked_field=BATCH_GAP_FILL)
    )
    counts = dict(thread.asked_counts or {})
    for key in keys:
        counts[key] = int(counts.get(key, 0)) + 1
    thread.asked_counts = counts
    update_flow_meta(thread, phase="gap_fill", gap_keys=keys)
    return TurnResult(
        ACTION_ASKED,
        thread,
        _coverage_for(thread),
        asked_field=BATCH_GAP_FILL,
        sent=[s for s in [sent] if s],
        extractor=thread.extractor,
    )


def _absorb_years(thread: IntakeThread, body: str) -> None:
    """IC follow-up: map '8 years' onto a seniority bucket in code, not by the model."""
    if thread.last_field_asked not in {BATCH_GAP_FILL, "spec.seniority"}:
        return
    if not wants_years_question(thread) and "spec.seniority" not in (
        flow_meta(thread).get("gap_keys") or []
    ):
        # Still try: they may have answered years unprompted in a batch.
        pass
    years = parse_years_experience(body)
    if years is None:
        return
    thread.record = apply_years_to_record(thread.record, years)
