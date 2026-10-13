"""Warm-up: a newly connected mailbox ramps up to its cap instead of starting there.

Mail providers judge a new sender by how its first weeks look. Rotation hands
each send to the least-used mailbox, so without this a fresh mailbox at the
default cap of 40 is the first to fill -- forty cold emails on day one, from an
address (often a domain) with no history at all.

``daily_send_cap`` stays what the person set and becomes the *ceiling*. While a
mailbox is warming, the cap in force is ``min(ceiling, ramp(day))``, where day
counts whole days since ``warmup_started_at``. Computed on every read, so there
is no job to run and nothing to drift: the ramp advances because time passes.
Lowering the cap below the ramp wins; raising it does not skip the ramp; 0
still means "send nothing". See README.md, failure items 32-36.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: (first day, cap from that day). After the last step the cap grows by
#: ``WEEKLY_STEP`` each further week until it reaches the mailbox's ceiling.
SCHEDULE: tuple[tuple[int, int], ...] = (
    (0, 2),
    (7, 4),
    (14, 7),
    (21, 10),
    (28, 15),
    (35, 20),
    (42, 25),
)
WEEKLY_STEP = 5


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def ramp_cap(day: int) -> int:
    """The most a warming mailbox may send on its ``day``-th day (0-based)."""
    day = max(int(day), 0)
    last_day, last_cap = SCHEDULE[-1]
    if day >= last_day:
        return last_cap + WEEKLY_STEP * ((day - last_day) // 7)
    cap = SCHEDULE[0][1]
    for start, value in SCHEDULE:
        if day >= start:
            cap = value
    return cap


def warmup_day(connection, now: datetime) -> int | None:
    started = _aware(getattr(connection, "warmup_started_at", None))
    if started is None:
        return None
    return max((_aware(now) - started).days, 0)


def effective_cap(connection, now: datetime) -> int:
    """The cap in force right now. Every reader of the cap goes through here."""
    ceiling = max(int(connection.daily_send_cap or 0), 0)
    day = warmup_day(connection, now)
    if day is None:
        return ceiling
    return min(ceiling, ramp_cap(day))


def _first_day_at(ceiling: int) -> int | None:
    """The first warm-up day whose ramp reaches ``ceiling``."""
    if ceiling <= 0:
        return 0
    for start, value in SCHEDULE:
        if value >= ceiling:
            return start
    last_day, last_cap = SCHEDULE[-1]
    weeks = -(-(ceiling - last_cap) // WEEKLY_STEP)  # ceil
    return last_day + 7 * weeks


def warmup_state(connection, now: datetime) -> dict:
    """What the UI says about a mailbox's warm-up."""
    day = warmup_day(connection, now)
    ceiling = max(int(connection.daily_send_cap or 0), 0)
    if day is None:
        return {"active": False, "day": None, "cap_today": ceiling, "full_on": None}
    reach = _first_day_at(ceiling)
    started = _aware(connection.warmup_started_at)
    active = day < reach
    return {
        "active": active,
        "day": day,
        "cap_today": min(ceiling, ramp_cap(day)),
        "full_on": (started + timedelta(days=reach)).date().isoformat(),
    }
