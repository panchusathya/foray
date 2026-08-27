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
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select
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
from . import cadence, connect_flow, contact_card, identity, linkedin_flow, prompts
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
    was_handed_off,
    with_send_key,
)
from .uploads import mint_upload_link

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

    # Above the STATUS_COMPLETE return, not below it. An opt-out is honoured
    # whatever state the thread is in: Linq handles STOP in its own webhook,
    # but the web widget has no other handler, so a STOP typed into a wrapped-up
    # conversation used to be discarded outright -- no suppression row, no
    # stopped thread, no acknowledgement, and the same visitor id greeted again
    # the next time they opened the widget.
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

    if thread.status == STATUS_COMPLETE:
        return TurnResult(ACTION_COMPLETED, thread, _coverage_for(thread))

    # Rule 2, hoisted above the flow branches so it covers all three. Their
    # turn: holding is the correct outcome, not a failure, and it is checked
    # before quiet hours because deferring a turn with nothing to send would
    # re-queue work for a message that was never going to exist.
    # A failed outbound is not an ask they saw. Counting it as "their turn"
    # froze the thread after a single carrier error: retries, quiet-hours
    # resumes and the next worker tick all held forever on a bubble nobody got.
    if last is not None and last.direction == OUTBOUND and last.status != "failed":
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

    # A job link can be the very first thing a stranger texts. Onboarding
    # still runs -- an application needs a name on it -- but the link is
    # remembered here and spent at hand-over. Managing threads skip the
    # stash: the manager's intent router owns the link on those.
    if (
        last is not None
        and last.direction == INBOUND
        and thread.mode != MODE_MANAGING
    ):
        from . import apply_link

        apply_link.stash_from_body(thread, body=last.body or "", message_id=last.id)

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
    # human to look even when every field that *was* answered looks clean, and
    # so is a duplicate-identity conflict the extractor cannot see. Assigning
    # both of these outright is what used to drop a thread off the review queue
    # one clean turn after `_copy_contact` found an address owned by another
    # candidate -- the conflict stayed live and nobody was ever shown it.
    thread.review_needed = (
        bool(result.review_needed)
        or not state.required_complete
        or identity.has_identity_conflict(thread)
    )
    if result.review_reason:
        thread.review_reason = identity.join_reasons(
            thread.review_reason, result.review_reason
        )
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


_REPEAT_VARIANTS = (
    "still the same as my last text — {body}",
    "no change since you last asked — {body}",
)


def _unrepeated(db: Session, *, thread: IntakeThread, body: str, verbatim: bool) -> str:
    """Never send the byte-identical bubble twice in a row.

    The last line of defence, on the one chokepoint every send crosses. The
    per-callsite mechanisms (paired "again" variants, ``manager._fresh``,
    ``humanize``) each cover their own callers; the live thread that answered
    every message with the same canned line for an afternoon proves a guard
    per callsite is a guard with gaps.

    An LLM rewrite says the same thing in different words; without a model (or
    for verbatim/URL-bearing copy, where a rewrite risks a fact) a canned
    preface makes the bubble different and names the repetition honestly. The
    comparison window is the last three outbound bubbles so the preface cannot
    itself alternate into a period-two loop. If every variant has also been
    said, the original goes out — a repeat is still better than silence.
    """
    candidate = (body or "").strip()
    if not candidate:
        return body
    recent: list[str] = []
    last_inbound = ""
    for message in reversed(messages_of(db, thread)):
        if message.direction == OUTBOUND and message.status != "failed" and len(recent) < 3:
            recent.append((message.body or "").strip().lower())
        elif message.direction == INBOUND and not last_inbound:
            last_inbound = message.body or ""
        if len(recent) >= 3 and last_inbound:
            break
    if not recent or candidate.lower() != recent[0]:
        return body
    if not verbatim and "http" not in candidate.lower():
        rewritten = _rewrite_repeat(body=body, last_inbound=last_inbound)
        if rewritten and rewritten.strip().lower() not in recent:
            return rewritten
    for template in _REPEAT_VARIANTS:
        variant = template.format(body=body)
        if variant.strip().lower() not in recent:
            return variant
    return body


def _rewrite_repeat(*, body: str, last_inbound: str) -> str:
    """One model pass to unstick a repeat; empty string when it cannot help."""
    llm = get_llm()
    if not llm.available:
        return ""
    try:
        result = llm.json_fast(
            system=prompts.REPEAT_SYSTEM,
            prompt=(
                f"The message about to repeat:\n{body}\n\n"
                f"They just said: {last_inbound.strip() or '(nothing)'}"
            ),
            schema=prompts.COMPOSE_SCHEMA,
        )
        written = str((result.data or {}).get("message") or "").strip()
    except Exception as exc:  # noqa: BLE001 - the canned variant ships instead
        logger.warning("repeat rewrite failed: %s", exc)
        return ""
    if not prompts.valid_question(written, batched=True):
        return ""
    if written.count("?") > body.count("?"):
        # An invented question gets a real answer nobody wanted.
        return ""
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
    body = _unrepeated(db, thread=thread, body=body, verbatim=message.verbatim)
    lowered = replace(message, body=body)
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
    recorded = record_outbound(db, thread=thread, message=outbound, result=result, origin=origin)
    if result.status == "sent":
        # After the message rather than before it, and only on a real send:
        # Linq refuses to share a contact card into a chat we have never
        # spoken in, and the row just written is what makes that untrue.
        contact_card.maybe_share(db, thread=thread)
    return recorded


def react_to_message(
    db: Session,
    *,
    thread: IntakeThread,
    message: IntakeMessage | None,
    reaction: str,
) -> bool:
    """Best-effort tapback on an existing message. Never raises.

    Reactions are courtesies — they intentionally bypass cadence budgets,
    like typing indicators. They never write a message row: a 👍 is not a
    turn, and counting it as outbound would freeze the "never two outbound
    in a row" bookkeeping on a reaction that is not a bubble.
    """
    from .suppression import is_blocked

    try:
        native_id = (getattr(message, "provider_native_id", None) or "").strip()
        if message is None or not native_id or not reaction:
            return False
        channel = get_channel(thread.channel)
        if not channel.is_configured():
            return False
        if channel.requires_consent() and not thread.consent_messaging:
            return False
        if is_blocked(db, thread=thread):
            return False
        return bool(channel.react(provider_message_id=native_id, reaction=reaction))
    except Exception as exc:  # noqa: BLE001 - courtesy; never fail a turn
        logger.warning("react_to_message failed on thread %s: %s", thread.id, exc)
        return False


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
        thread.review_reason = identity.join_reasons(
            thread.review_reason, identity.UNATTRIBUTED_REASON
        )

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
        thread.review_reason = identity.join_reasons(
            thread.review_reason, identity.UNATTRIBUTED_REASON
        )

    thread.mode = MODE_MANAGING
    db.flush()
    # A link that arrived mid-onboarding is spent the moment the thread has
    # a candidate to apply for.
    from . import apply_link

    apply_link.ensure_enqueued(db, thread)
    # Not composed, on purpose. This line lists the literal phrases the intent
    # router matches on -- "what's my status", "find me roles", "who should I
    # talk to". A paraphrase teaches somebody a command that then does not
    # work, which is worse than sounding slightly flat once.
    sent = _deliver(db, thread=thread, message=OutboundMessage(body=prompts.MANAGING_READY))
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
    outbound_sent = any(
        m.direction == OUTBOUND and was_handed_off(m) for m in history
    )

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
        # A resume that arrived before the thread had an identity is waiting
        # in flow meta; the absorb above may have just captured the email that
        # lets it attach. No-op on every other turn.
        from .attachments import materialize_pending

        materialize_pending(db, thread=thread)

    # The intro fires once, on a thread that has never spoken and was never
    # asked anything. It used to key on "no email yet" — the email was the
    # intro's own question — but the intro no longer asks for one, so the
    # guards are the conversation's actual state: nothing sent, nothing asked,
    # nobody known.
    if last is None or (
        last.direction == INBOUND
        and not outbound_sent
        and not (thread.last_field_asked or "").strip()
        and not thread.candidate_id
    ):
        return _linkedin_intro(db, thread=thread)

    if last is not None and last.direction == OUTBOUND and last.status != "failed":
        return TurnResult(ACTION_HELD, thread, _coverage_for(thread))

    assert last is not None

    # A resume is stashed but nothing links this thread to a person yet —
    # nothing can move until an email (or LinkedIn URL) arrives, so ask for
    # exactly that instead of falling into the profile ask they already
    # answered with a file.
    from .attachments import PENDING_KEY

    if flow_meta(thread).get(PENDING_KEY) and not (thread.contact_email or "").strip():
        if thread.last_field_asked == identity.CONTACT_EMAIL:
            body = "still need an email to attach your resume to — what's the best one?"
        else:
            body = (
                "got your resume. what's the best email to reach you on, "
                "so i can attach it to your file?"
            )
        sent = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(body=body, asked_field=identity.CONTACT_EMAIL),
        )
        return TurnResult(
            ACTION_ASKED,
            thread,
            _coverage_for(thread),
            asked_field=identity.CONTACT_EMAIL,
            sent=[s for s in [sent] if s],
        )

    # An index of a connected app is still running. The same "wait for it"
    # state a scrape or a parsing resume is, and it must be checked BEFORE the
    # profile ask below: a connection carries no LinkedIn URL and no document,
    # so without this hold the next inbound re-sends an ask the tap already
    # answered. The index job's completion (or its catch-everything failure
    # path) enqueues the turn that speaks next.
    if connect_flow.connect_pending(db, thread):
        return TurnResult(ACTION_HELD, thread, _coverage_for(thread))

    # A URL or a resume; either one is a way to read somebody. The resume is
    # the better of the two and has been all along -- it carries dated roles,
    # skills and a summary, where a profile scrape carries a headline. The
    # matcher scores the candidate's own record against what an employer wants,
    # so a candidate onboarded from a header misses every skill and keyword
    # criterion there is. The upload page and its parse already existed; the
    # onboarding flow simply never offered them.
    #
    # ``not _scrape_settled``: a connector index can settle the profile step
    # with neither a URL nor a document on the thread, and re-asking somebody
    # whose data we already read is the agent not reading its own notes.
    if (
        not thread.contact_linkedin
        and not _profile_document(db, thread)
        and not _scrape_settled(thread)
    ):
        # Connect-your-apps replaces the ask when it can be offered: the same
        # message still carries the LinkedIn/resume alternative, so the offer
        # costs zero extra turns and declining needs no parsing — whatever
        # arrives next lands in a path that already existed. One offer per
        # thread, ever; after that this branch falls through to gap-fill via
        # the settlement routing rather than re-pitching or re-asking.
        if connect_flow.should_offer(db, thread):
            return _connect_offer(db, thread=thread)
        if thread.last_field_asked != connect_flow.BATCH_CONNECT:
            return _ask_profile(db, thread=thread)

    # A resume that has landed but not finished parsing is the same "wait for
    # it" state a scrape is, and a resume that HAS parsed settles the profile
    # step outright -- there is nothing left to scrape for.
    if not _scrape_settled(thread):
        document = _profile_document(db, thread)
        if document is not None:
            if document.status != "ready":
                return TurnResult(ACTION_HELD, thread, _coverage_for(thread))
        elif thread.contact_linkedin:
            _enqueue_linkedin_scrape(db, thread)
            return TurnResult(ACTION_HELD, thread, _coverage_for(thread))

    _reread(db, thread=thread)
    _absorb_years(thread, last.body)
    if flow_meta(thread).get("scrape") and not thread.contact_name:
        name = str(scraped_profile(thread).get("full_name") or "").strip()
        if name:
            thread.contact_name = name[:240]

    asked = thread.last_field_asked or ""
    # BATCH_CONNECT routes with the profile asks: a settled index answers the
    # connect offer the same way a scrape answers the URL ask, and without it
    # a successful connect would fall to _wrap_up and skip its confirm.
    if asked in ("", CONTACT_LINKEDIN, identity.CONTACT_EMAIL, connect_flow.BATCH_CONNECT):
        return _linkedin_after_scrape(db, thread=thread, last=last)
    if asked == BATCH_CONFIRM:
        return _linkedin_after_confirm(db, thread=thread, last=last)
    if asked == linkedin_flow.BATCH_RETARGET:
        return _linkedin_after_retarget(db, thread=thread, last=last)
    return _wrap_up(db, thread=thread, state=_coverage_for(thread))


def _scrape_settled(thread: IntakeThread) -> bool:
    meta = flow_meta(thread)
    return bool(meta.get("scrape") or meta.get("error"))


def _profile_document(db: Session, thread: IntakeThread):
    """The resume this thread uploaded, whatever state it is in.

    Keyed on the thread rather than the candidate: a resume the candidate
    uploaded for a previous application is not an answer to the question this
    conversation just asked.
    """
    from ..models import CandidateDocument

    return db.scalar(
        select(CandidateDocument)
        .where(
            CandidateDocument.thread_id == thread.id,
            CandidateDocument.kind == "resume_uploaded",
        )
        .order_by(CandidateDocument.created_at.desc())
    )


def _linkedin_intro(db: Session, *, thread: IntakeThread) -> TurnResult:
    """The intro bubble, then the profile ask riding the same turn.

    The intro is context, not a question — it used to end on "what's the best
    email to reach you on?", which made a stranger's first task typing an
    address before anything was in it for them. The email now arrives on its
    own from the OAuth callback (verified, no typing), or in the gap-fill for
    threads that went the manual route.
    """
    from .attachments import PENDING_KEY

    org = db.get(Org, thread.org_id)
    org_name = (getattr(org, "name", "") or "our team").strip()
    body = intro_message(recruiter=prompts.recruiter_name(), org=org_name)
    intro_sent = _deliver(db, thread=thread, message=OutboundMessage(body=body))
    update_flow_meta(thread, phase="intro")
    if thread.contact_linkedin:
        # The first message WAS the profile — a pasted URL. Asking for one
        # anyway is the agent not reading; scrape it and hold.
        _enqueue_linkedin_scrape(db, thread)
        follow = TurnResult(ACTION_HELD, thread, _coverage_for(thread))
    elif flow_meta(thread).get(PENDING_KEY):
        # Same for a resume attached as the opener: the only thing missing is
        # an address to attach it to.
        ask = (
            "got your resume. what's the best email to reach you on, "
            "so i can attach it to your file?"
        )
        sent = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(body=ask, asked_field=identity.CONTACT_EMAIL),
        )
        follow = TurnResult(
            ACTION_ASKED,
            thread,
            _coverage_for(thread),
            asked_field=identity.CONTACT_EMAIL,
            sent=[s for s in [sent] if s],
        )
    elif connect_flow.should_offer(db, thread):
        follow = _connect_offer(db, thread=thread)
    else:
        follow = _ask_profile(db, thread=thread)
    follow.sent[:0] = [s for s in [intro_sent] if s]
    return follow


def _ask_profile(db: Session, *, thread: IntakeThread) -> TurnResult:
    """The resume/LinkedIn ask, worded per channel.

    iMessage carries files, so on Linq the ask is "attach it right here"; the
    upload page stays the route for web chat, which cannot, and reappears on
    Linq only as the fallback when a download or a parse fails.
    """
    from .channels import LINQ

    if thread.channel == LINQ:
        body = prompts.ASK_RESUME_OR_LINKEDIN
    else:
        body = prompts.ASK_LINKEDIN.format(upload_link=mint_upload_link(thread))
    sent = _deliver(
        db,
        thread=thread,
        message=OutboundMessage(body=body, asked_field=CONTACT_LINKEDIN),
    )
    return TurnResult(
        ACTION_ASKED,
        thread,
        _coverage_for(thread),
        asked_field=CONTACT_LINKEDIN,
        sent=[s for s in [sent] if s],
    )


def _connect_offer(db: Session, *, thread: IntakeThread) -> TurnResult:
    """The combined profile ask: connect an app, or send LinkedIn/resume.

    Canned and batched (two routes cannot fit the single-question budget), so
    it goes out as written — the same rule the confirm and gap-fill messages
    follow. Marked offered before sending: a delivery retry must not turn one
    pitch into two.
    """
    connect_flow.mark_offered(thread)
    body = connect_flow.offer_message(thread)
    sent = _deliver(
        db,
        thread=thread,
        message=OutboundMessage(body=body, asked_field=connect_flow.BATCH_CONNECT),
    )
    return TurnResult(
        ACTION_ASKED,
        thread,
        _coverage_for(thread),
        asked_field=connect_flow.BATCH_CONNECT,
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
    # Scrape (or resume parse, or connector index) failed — skip confirm and
    # go straight to the leftover questions. The copy names what actually
    # happened: "couldn't pull your linkedin" to somebody who sent a file is
    # the agent not listening, and an apology to somebody who simply passed
    # on the connect offer is answering an error nobody hit.
    source = str(meta.get("source") or "")
    if source == "resume":
        preface = linkedin_flow.resume_failed_message()
    elif source.startswith("connector"):
        preface = connect_flow.connect_failed_message()
    elif thread.last_field_asked == connect_flow.BATCH_CONNECT and not meta.get("error"):
        preface = connect_flow.declined_preface()
    else:
        preface = scrape_failed_message()
    return _linkedin_gap_fill(db, thread=thread, last=last, preface=preface)


def _linkedin_after_confirm(
    db: Session, *, thread: IntakeThread, last: IntakeMessage
) -> TurnResult:
    """Confirmed the profile -> straight into managing. No closing survey.

    The confirm answer is the last question onboarding asks. Whatever the
    candidate said here has already gone through ``_reread``, so a correction
    ("no, strategic finance") is in the record before we hand over, and the
    gap-fill that used to run next asked for comp and timing -- application
    questions, asked again at package time, and asked here of somebody who has
    not yet been shown a single role.

    ``short_missing`` still decides one thing: whether we know enough to search
    at all. With no title even the confirm cannot have worked, so those threads
    still get one question rather than a promise we cannot keep.
    """
    # The docstring above assumed `_reread` always catches a correction. Live,
    # "No I want to switch to Strat fin" left job_titles as ["Investor"] and
    # the thread answered "you're set" -- so we asked, ignored the answer, and
    # went looking for the job they had just rejected. Asking a question and
    # discarding the reply is worse than never asking.
    if linkedin_flow.looks_like_correction(last.body) and linkedin_flow.target_unchanged(
        thread
    ):
        return _linkedin_retarget(db, thread=thread, last=last)

    missing = linkedin_flow.short_missing(thread)
    if missing:
        return _linkedin_gap_fill(db, thread=thread, last=last)
    return _wrap_up(db, thread=thread, state=_coverage_for(thread))


def _linkedin_retarget(
    db: Session, *, thread: IntakeThread, last: IntakeMessage
) -> TurnResult:
    """They rejected the inferred target and we could not read the replacement."""
    body = humanize(thread, says=linkedin_flow.RETARGET_LINE, last_inbound=last.body or "")
    sent = _deliver(
        db,
        thread=thread,
        message=OutboundMessage(body=body, asked_field=linkedin_flow.BATCH_RETARGET),
    )
    return TurnResult(
        ACTION_ASKED,
        thread,
        _coverage_for(thread),
        asked_field=linkedin_flow.BATCH_RETARGET,
        sent=[s for s in [sent] if s],
        extractor=thread.extractor,
    )


def _linkedin_after_retarget(
    db: Session, *, thread: IntakeThread, last: IntakeMessage
) -> TurnResult:
    """Bind the answer to the target, then carry on.

    `_reread` has already run and usually catches a direct answer to a direct
    question. When it does not, the reply is taken as the title verbatim --
    safe here for the same reason `find_name` is safe only after the name
    question: we asked exactly this, so the whole message is the answer.
    """
    if linkedin_flow.target_unchanged(thread):
        stated = linkedin_flow.title_from_reply(last.body or "")
        if stated:
            thread.record = linkedin_flow.apply_title_to_record(thread.record, stated)
            logger.info("thread %s retargeted to %r by hand", thread.id, stated)
        else:
            # Still nothing usable, but we know the scraped title is wrong --
            # they rejected it twice now. Clearing it puts spec.job_titles back
            # in `short_missing`, so the gap-fill asks properly instead of us
            # searching for a job they have said no to.
            thread.record = linkedin_flow.clear_target(thread.record)
            logger.info("thread %s: target rejected, cleared for gap-fill", thread.id)
        db.flush()

    missing = linkedin_flow.short_missing(thread)
    if missing:
        return _linkedin_gap_fill(db, thread=thread, last=last)
    return _wrap_up(db, thread=thread, state=_coverage_for(thread))


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
    # Keep the number, not just the band it maps to. `spec.seniority` is what
    # the conversation needed; `Candidate.years_experience` is what the matcher
    # scores against a posting's stated minimum, and a thread intake has no
    # Experience rows to compute it from -- so without this every text-onboarded
    # candidate is "0 years of tracked experience" forever.
    update_flow_meta(thread, years=years)
