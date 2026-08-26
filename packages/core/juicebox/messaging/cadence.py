"""Quiet hours and outbound budgets — CANDIDATE_AGENT_PLAN.md §3 and §4 in code.

TCPA restricts solicitation to 8am-9pm in the *recipient's* local time, and we
do not reliably know where the recipient is. The design here follows the plan:

* **A reply is not a solicitation.** An outbound message answering an inbound
  one received within the last ``INTAKE_REPLY_WINDOW_MINUTES`` sends regardless
  of the clock — somebody who texts at 11pm expects an answer, and holding it
  until 11am serves nobody the rule protects.
* **Unknown timezone → intersect the windows instead of guessing.** The only
  safe window for "anywhere in the contiguous US" is 8am-9pm across Eastern
  *and* Pacific at once, which is stable in both DST regimes when expressed in
  Eastern local time: **11:00-21:00 America/New_York**. Alaska/Hawaii would
  narrow it further, which is why the bounds are config values rather than
  constants.
* **Store how the zone was resolved.** A window derived from a stated location
  and one derived from nothing deserve different confidence, and when a
  complaint arrives you need to know which you used.

Deferral reuses the queue: the caller re-enqueues with ``run_at`` set to
``CadenceDecision.resume_at``. There is deliberately no scheduler here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..enrich.taxonomy import normalize_location
from ..models import Candidate, IntakeThread, utcnow
from .threads import HANDED_OFF, STATUS_ACTIVE, STATUS_STOPPED

logger = logging.getLogger(__name__)

#: Any zone works for a 24-hour window; UTC avoids a lookup.
UTC_ZONE = ZoneInfo("UTC")

# The real TCPA window, applied when the recipient's zone is known.
QUIET_LOCAL_START_HOUR = 8
QUIET_LOCAL_END_HOUR = 21

# Nudge budget, decided in MESSAGING_INTAKE_PLAN and restated in
# CANDIDATE_AGENT_PLAN §4.2: first nudge at 48h of silence, at most two per
# thread, at least 72h apart, never after a wrap-up or an opt-out. The budget
# is counted from message rows (origin="nudge"), not a counter column, so it
# cannot drift from what was actually sent. Notifications write
# origin="proactive" and are deliberately NOT counted: an application-sent
# text is event-driven and idempotent per event, and holding the third one
# because two earlier notifications "spent the nudge budget" silenced exactly
# the messages candidates most expect.
NUDGE_FIRST_AFTER_HOURS = 48
MAX_NUDGES_PER_THREAD = 2
NUDGE_MIN_GAP_HOURS = 72

# What kind of initiated send `may_initiate` is deciding. A nudge ("are you
# still there?") carries the full nudge budget; a notification (application
# sent, matches ready) is gated by quiet hours and the org circuit breaker
# only — its idempotency lives in the event key, not a per-thread counter.
INITIATIVE_NOTIFY = "notify"
INITIATIVE_NUDGE = "nudge"

#: IntakeMessage.origin for a nudge row — distinct from "proactive" so the
#: nudge budget charges for nudges and nothing else.
ORIGIN_NUDGE = "nudge"

# How the thread's timezone was resolved, best source first. "area_code" is
# reserved: the plan allows it as a tie-breaker but "not good enough to
# authorise a 7am send", so until someone builds the NANP table the unknown
# window simply applies — conservative is always correct.
TZ_SOURCE_STATED = "stated_location"
TZ_SOURCE_CANDIDATE = "candidate_record"
TZ_SOURCE_ASKED = "asked"

REASON_REPLY_WINDOW = "reply-window"
REASON_IN_WINDOW = "in-window"
REASON_QUIET_HOURS = "quiet-hours"
REASON_ORG_CIRCUIT_OPEN = "org-circuit-open"
REASON_NUDGE_BUDGET = "nudge-budget"
REASON_NUDGE_TOO_SOON = "nudge-too-soon"
REASON_THREAD_NOT_ACTIVE = "thread-not-active"


@dataclass(frozen=True)
class CadenceDecision:
    """Whether an outbound message may go now, and if not, when to try again.

    ``resume_at`` is set only for time-based refusals (quiet hours). A budget
    or circuit refusal has no retry time — retrying does not change it.
    """

    allowed: bool
    reason: str
    resume_at: datetime | None = None
    timezone: str = ""
    timezone_source: str = ""


# ---------------------------------------------------------------------------
# timezone resolution
# ---------------------------------------------------------------------------

# Dominant IANA zone per US state, plus the metro cities the taxonomy already
# knows. Split states (Indiana, Kentucky, Tennessee…) get their population-
# dominant zone: this feeds a *send window*, not a clock, and the unknown-zone
# fallback below stays the safety net for anything not listed.
_STATE_TZ = {
    "alabama": "America/Chicago",
    "alaska": "America/Anchorage",
    "arizona": "America/Phoenix",
    "arkansas": "America/Chicago",
    "california": "America/Los_Angeles",
    "colorado": "America/Denver",
    "connecticut": "America/New_York",
    "delaware": "America/New_York",
    "district of columbia": "America/New_York",
    "florida": "America/New_York",
    "georgia": "America/New_York",
    "hawaii": "Pacific/Honolulu",
    "idaho": "America/Denver",
    "illinois": "America/Chicago",
    "indiana": "America/New_York",
    "iowa": "America/Chicago",
    "kansas": "America/Chicago",
    "kentucky": "America/New_York",
    "louisiana": "America/Chicago",
    "maine": "America/New_York",
    "maryland": "America/New_York",
    "massachusetts": "America/New_York",
    "michigan": "America/New_York",
    "minnesota": "America/Chicago",
    "mississippi": "America/Chicago",
    "missouri": "America/Chicago",
    "montana": "America/Denver",
    "nebraska": "America/Chicago",
    "nevada": "America/Los_Angeles",
    "new hampshire": "America/New_York",
    "new jersey": "America/New_York",
    "new mexico": "America/Denver",
    "new york": "America/New_York",
    "north carolina": "America/New_York",
    "north dakota": "America/Chicago",
    "ohio": "America/New_York",
    "oklahoma": "America/Chicago",
    "oregon": "America/Los_Angeles",
    "pennsylvania": "America/New_York",
    "rhode island": "America/New_York",
    "south carolina": "America/New_York",
    "south dakota": "America/Chicago",
    "tennessee": "America/Chicago",
    "texas": "America/Chicago",
    "utah": "America/Denver",
    "vermont": "America/New_York",
    "virginia": "America/New_York",
    "washington": "America/Los_Angeles",
    "west virginia": "America/New_York",
    "wisconsin": "America/Chicago",
    "wyoming": "America/Denver",
}

_CITY_TZ = {
    # Pacific
    "san francisco": "America/Los_Angeles",
    "oakland": "America/Los_Angeles",
    "berkeley": "America/Los_Angeles",
    "palo alto": "America/Los_Angeles",
    "mountain view": "America/Los_Angeles",
    "menlo park": "America/Los_Angeles",
    "san jose": "America/Los_Angeles",
    "sunnyvale": "America/Los_Angeles",
    "san mateo": "America/Los_Angeles",
    "redwood city": "America/Los_Angeles",
    "santa clara": "America/Los_Angeles",
    "bay area": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "san diego": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "bellevue": "America/Los_Angeles",
    "redmond": "America/Los_Angeles",
    "kirkland": "America/Los_Angeles",
    "portland": "America/Los_Angeles",
    "las vegas": "America/Los_Angeles",
    # Mountain
    "denver": "America/Denver",
    "boulder": "America/Denver",
    "salt lake city": "America/Denver",
    "phoenix": "America/Phoenix",
    "scottsdale": "America/Phoenix",
    "tempe": "America/Phoenix",
    # Central
    "austin": "America/Chicago",
    "dallas": "America/Chicago",
    "houston": "America/Chicago",
    "san antonio": "America/Chicago",
    "chicago": "America/Chicago",
    "minneapolis": "America/Chicago",
    "nashville": "America/Chicago",
    "new orleans": "America/Chicago",
    "kansas city": "America/Chicago",
    "st louis": "America/Chicago",
    # Eastern
    "new york": "America/New_York",
    "brooklyn": "America/New_York",
    "queens": "America/New_York",
    "manhattan": "America/New_York",
    "jersey city": "America/New_York",
    "hoboken": "America/New_York",
    "boston": "America/New_York",
    "cambridge": "America/New_York",
    "somerville": "America/New_York",
    "philadelphia": "America/New_York",
    "pittsburgh": "America/New_York",
    "washington dc": "America/New_York",
    "miami": "America/New_York",
    "atlanta": "America/New_York",
    "charlotte": "America/New_York",
    "raleigh": "America/New_York",
    # Non-US metros the taxonomy knows. Not TCPA territory, but a resolved
    # zone still beats texting Bangalore at 4am local.
    "london": "Europe/London",
    "berlin": "Europe/Berlin",
    "bangalore": "Asia/Kolkata",
    "toronto": "America/Toronto",
    "singapore": "Asia/Singapore",
}


def _zone_for_location(value: str) -> str:
    slug = normalize_location(value)
    if not slug:
        return ""
    return _CITY_TZ.get(slug) or _STATE_TZ.get(slug) or ""


def resolve_timezone(db: Session, *, thread: IntakeThread) -> tuple[str, str]:
    """The thread's send-window zone and how we learned it.

    Best source first, per the plan: a zone already stored (including one the
    candidate stated when asked), the locations they told intake about, then
    the candidate record. Resolution is persisted on the thread so the answer
    is stable and auditable — the source column is what tells a complaint
    review whether the window came from a stated location or a guess.
    """
    if thread.timezone:
        return thread.timezone, thread.timezone_source or TZ_SOURCE_STATED

    record = thread.record or {}
    spec = record.get("spec") or {}
    for value in spec.get("locations") or []:
        if not isinstance(value, str):
            continue
        zone = _zone_for_location(value)
        if zone:
            _store(db, thread, zone, TZ_SOURCE_STATED)
            return zone, TZ_SOURCE_STATED

    if thread.candidate_id:
        candidate = db.get(Candidate, thread.candidate_id)
        if candidate is not None:
            for value in (candidate.location_city, candidate.location_region):
                zone = _zone_for_location(value or "")
                if zone:
                    _store(db, thread, zone, TZ_SOURCE_CANDIDATE)
                    return zone, TZ_SOURCE_CANDIDATE

    return "", ""


def _store(db: Session, thread: IntakeThread, zone: str, source: str) -> None:
    thread.timezone = zone
    thread.timezone_source = source
    db.flush()


# ---------------------------------------------------------------------------
# quiet hours
# ---------------------------------------------------------------------------


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything here compares in UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _window_bounds(zone_name: str) -> tuple[int, int, ZoneInfo]:
    """(start_hour, end_hour, zone) for the send window.

    With ``QUIET_HOURS_ENABLED`` off the window is the whole day, which makes
    ``in_send_window`` always true and ``next_window_open`` always now -- the
    deferral path stops firing without any caller needing to know. Gated here
    rather than at each call site so there is one place to look.
    """
    settings = get_settings()
    if not settings.quiet_hours_enabled:
        return 0, 24, UTC_ZONE
    if zone_name:
        try:
            return QUIET_LOCAL_START_HOUR, QUIET_LOCAL_END_HOUR, ZoneInfo(zone_name)
        except ZoneInfoNotFoundError:
            logger.warning("unknown timezone %r on thread, using fallback window", zone_name)
    return (
        settings.quiet_hours_unknown_start_hour,
        settings.quiet_hours_unknown_end_hour,
        ZoneInfo(settings.quiet_hours_unknown_zone),
    )


def in_send_window(now: datetime, *, zone_name: str = "") -> bool:
    start_hour, end_hour, zone = _window_bounds(zone_name)
    local = now.astimezone(zone)
    return start_hour <= local.hour < end_hour


def next_window_open(now: datetime, *, zone_name: str = "") -> datetime:
    """The next UTC instant the send window opens. ``now`` if already open."""
    start_hour, end_hour, zone = _window_bounds(zone_name)
    local = now.astimezone(zone)
    if start_hour <= local.hour < end_hour:
        return now
    opens = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if local.hour >= end_hour:
        opens = opens + timedelta(days=1)
    return opens.astimezone(UTC)


def may_send_now(db: Session, *, thread: IntakeThread, now: datetime | None = None) -> CadenceDecision:
    """May this thread send a *reply-path* message right now?

    Called from ``conversation.run_turn`` before composing, for the same
    reason ``suppression.is_blocked`` is: rendering a message we then hold
    still costs a model call. The reply window is checked first — quiet hours
    govern messages we initiate, not answers to somebody mid-conversation.
    """
    now = now or utcnow()
    settings = get_settings()

    last_inbound = _aware(thread.last_inbound_at)
    if last_inbound is not None:
        window = timedelta(minutes=settings.intake_reply_window_minutes)
        if now - last_inbound <= window:
            return CadenceDecision(True, REASON_REPLY_WINDOW)

    zone, source = resolve_timezone(db, thread=thread)
    if in_send_window(now, zone_name=zone):
        return CadenceDecision(True, REASON_IN_WINDOW, timezone=zone, timezone_source=source)
    return CadenceDecision(
        False,
        REASON_QUIET_HOURS,
        resume_at=next_window_open(now, zone_name=zone),
        timezone=zone,
        timezone_source=source,
    )


# ---------------------------------------------------------------------------
# initiated outbound: nudges, proposals, notifications
# ---------------------------------------------------------------------------


def org_outbound_allowed(db: Session, *, org_id: str, now: datetime | None = None) -> bool:
    """The per-org complaint circuit breaker (CANDIDATE_AGENT_PLAN §2).

    One shared number couples every tenant's reputation, and opt-out rate is
    the number carriers actually score. Past the daily threshold this org's
    *initiated* outbound stops; replies to an active conversation are not
    gated here, because answering somebody is not what trips carrier filters.
    """
    now = now or utcnow()
    settings = get_settings()
    since = now - timedelta(hours=24)
    count = (
        db.scalar(
            select(func.count())
            .select_from(IntakeThread)
            .where(
                IntakeThread.org_id == org_id,
                IntakeThread.status == STATUS_STOPPED,
                IntakeThread.completed_at >= since,
            )
        )
        or 0
    )
    if count >= settings.messaging_optout_breaker_per_day:
        logger.warning(
            "org %s outbound paused: %d opt-outs in 24h (threshold %d)",
            org_id,
            count,
            settings.messaging_optout_breaker_per_day,
        )
        return False
    return True


def _nudge_rows(db: Session, thread: IntakeThread) -> list[datetime]:
    from ..models import IntakeMessage  # local import mirrors worker style

    rows = db.scalars(
        select(IntakeMessage.sent_at)
        .where(
            IntakeMessage.thread_id == thread.id,
            IntakeMessage.origin == ORIGIN_NUDGE,
            # Handed off, not literally "sent": a delivery receipt rewrites the
            # column to delivered/read, and matching on "sent" alone made every
            # nudge the carrier confirmed vanish from its own budget -- so the
            # gate reopened and the budget could never be spent.
            IntakeMessage.status.in_(HANDED_OFF),
        )
        .order_by(IntakeMessage.seq)
    ).all()
    return [ts for ts in (_aware(t) for t in rows) if ts is not None]


def _nudge_budget(
    db: Session, *, thread: IntakeThread, now: datetime
) -> CadenceDecision | None:
    """The nudge-only gates, or ``None`` when the nudge may proceed to quiet
    hours. CANDIDATE_AGENT_PLAN §4.2: first at 48h of silence, at most two per
    thread, at least 72h apart, never after a wrap-up or an opt-out.
    """
    # "Never after a wrap-up or an opt-out": a completed or stopped thread has
    # nothing to nudge back to life.
    if thread.status != STATUS_ACTIVE:
        return CadenceDecision(False, REASON_THREAD_NOT_ACTIVE)

    # Silence is measured from *their* last message: our own outbound does not
    # reset it, or a reply we sent would postpone the nudge it precedes.
    last_heard = _aware(thread.last_inbound_at) or _aware(thread.created_at) or now
    quiet_since = timedelta(hours=NUDGE_FIRST_AFTER_HOURS)
    if now - last_heard < quiet_since:
        return CadenceDecision(
            False, REASON_NUDGE_TOO_SOON, resume_at=last_heard + quiet_since
        )

    sent = _nudge_rows(db, thread)
    if len(sent) >= MAX_NUDGES_PER_THREAD:
        return CadenceDecision(False, REASON_NUDGE_BUDGET)
    if sent:
        gap = timedelta(hours=NUDGE_MIN_GAP_HOURS)
        if now - sent[-1] < gap:
            return CadenceDecision(False, REASON_NUDGE_TOO_SOON, resume_at=sent[-1] + gap)
    return None


def may_initiate(
    db: Session,
    *,
    thread: IntakeThread,
    now: datetime | None = None,
    kind: str = INITIATIVE_NOTIFY,
) -> CadenceDecision:
    """May we send a message nobody asked for — a nudge, proposal, or notify?

    Everything ``may_send_now`` checks except the reply window (there is no
    reply to be in the window of), plus the org circuit breaker. A nudge
    (``kind=INITIATIVE_NUDGE``) additionally carries the whole nudge budget —
    48h of silence first, at most two, 72h apart. A notification does not:
    its dedup lives in the event-keyed job, and the budget held here used to
    silence the third application-sent text on a thread, which is why
    ``NUDGE_FIRST_AFTER_HOURS`` could never be enforced in this gate before
    the two kinds were split.

    Count refusals carry no ``resume_at`` — waiting does not restore a spent
    budget. Time refusals (too soon, gap) do, so a scheduler can requeue.
    """
    now = now or utcnow()

    if not org_outbound_allowed(db, org_id=thread.org_id, now=now):
        return CadenceDecision(False, REASON_ORG_CIRCUIT_OPEN)

    if kind == INITIATIVE_NUDGE:
        refused = _nudge_budget(db, thread=thread, now=now)
        if refused is not None:
            return refused

    zone, source = resolve_timezone(db, thread=thread)
    if in_send_window(now, zone_name=zone):
        return CadenceDecision(True, REASON_IN_WINDOW, timezone=zone, timezone_source=source)
    return CadenceDecision(
        False,
        REASON_QUIET_HOURS,
        resume_at=next_window_open(now, zone_name=zone),
        timezone=zone,
        timezone_source=source,
    )
