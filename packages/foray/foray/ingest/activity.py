"""Real activity dates for a LinkedIn record, decoded from feed URNs.

WHY THIS EXISTS. ``candidate_signals.activity_at`` used to be the import
wall-clock for every LinkedIn row: ``ingest/linkedin_file.py`` stamps
``fetched_at = now()`` on insert *and* on conflict-update, and
``enrich/derive.py`` copied that into ``activity_at`` with
``activity_kind='snapshot'``. Measured on prod: **196,778 / 196,778** LinkedIn
rows passed the 24-month recency floor, all stamped ``snapshot`` inside a
single month. The recency clause in the search filter and in all 15 archetype
gates was therefore a no-op for 96% of the corpus. This module is where real
activity evidence comes from instead.

THE DISCOVERY. LinkedIn's ``activity[].id`` is a Snowflake-style URN: a 64-bit
integer whose high bits are a **millisecond Unix epoch at bit-shift 22**
(``int(id) >> 22``). The same encoding holds for ``posts[].id``.

VERIFIED against the real 2 GB Bright Data export (8,000-record sample):

  * 48.7% of records carry at least one decodable ``activity[].id``;
  * 92.2% of individual ids decode into a plausible window;
  * the latest-activity year histogram is 2026:2529, 2025:484, 2024:245,
    2023:186 ... 2015:11 — the long-tail shape of real human activity, not
    the flat smear that noise or a clock would produce;
  * the worked example ``7488687697968975873`` decodes to 2026-07-30, which
    matches the ``1785442278425`` millisecond stamp embedded in that same
    activity item's CDN image URL.
  * ``posts[].created_at`` covers only 2.4% of records, hence the ladder
    below puts it second.

THIS FORMAT IS UNDOCUMENTED. LinkedIn publishes no spec for the URN layout;
the bit-shift was recovered empirically. If LinkedIn changes the encoding, ids
start decoding to absurd dates — so the **plausibility window is the guard**:
anything outside 2015-01-01 .. now+35d is rejected as "not a date", the
function returns None, and the caller falls back to "unknown activity" rather
than writing a fabricated timestamp. Silence is the failure mode we want;
inventing recency is the one we are removing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

# Bit-shift recovered from the export (see the module docstring): the low 22
# bits are sequence/worker bits, the rest is a millisecond epoch.
URN_TIMESTAMP_SHIFT = 22

# Plausibility window. Lower bound: LinkedIn's current feed-URN scheme does not
# predate this in the export (the oldest decoded activity is 2015), so an
# "older" decode means we decoded garbage, not an old post. Upper bound: a
# small forward slack absorbs clock skew between LinkedIn and this machine
# without admitting the far-future values a changed encoding would produce.
URN_EPOCH_FLOOR = datetime(2015, 1, 1, tzinfo=UTC)
URN_FUTURE_SLACK = timedelta(days=35)

_MS_PER_SECOND = 1000.0

# posts[].created_at is NOT ISO-8601. Measured across the whole sample it is
# always JavaScript's Date.prototype.toString() form:
#   "Sat Nov 26 2016 00:00:00 GMT+0000 (Coordinated Universal Time)"
# 625/625 distinct values in the 8,000-record sample matched this one shape.
# linkedin_map.parse_month cannot read it — that parser is built for the
# *experience* fields ("Jul 2017" / "2017" / "Present"), a two-token month-year
# grammar with no weekday, no day-of-month and no time — so this module keeps
# its own tiny parser and reuses only linkedin_map's month table, which is the
# part that genuinely fits.
_JS_DATE_RE = re.compile(
    r"^\w{3}\s+(?P<mon>\w{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})"
    r"(?:\s+(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}))?"
)


def _in_window(dt: datetime) -> bool:
    return URN_EPOCH_FLOOR <= dt <= datetime.now(UTC) + URN_FUTURE_SLACK


def decode_activity_ts(urn_id: Any) -> datetime | None:
    """Decode a LinkedIn activity/post URN id into its UTC timestamp.

    None for anything unparseable or implausible — see the module docstring on
    why an out-of-window decode must read as "unknown", never as a date.
    """
    if urn_id is None or isinstance(urn_id, bool):
        return None
    text = str(urn_id).strip()
    if not text:
        return None
    # Tolerate a fully-qualified URN ("urn:li:activity:7488...") as well as the
    # bare numeric id the export actually ships.
    text = text.rsplit(":", 1)[-1]
    try:
        raw = int(text)
    except ValueError:
        return None
    if raw <= 0:
        return None
    ms = raw >> URN_TIMESTAMP_SHIFT
    try:
        dt = datetime.fromtimestamp(ms / _MS_PER_SECOND, UTC)
    except (OverflowError, OSError, ValueError):
        # A 64-bit id whose shifted value overflows the platform's time_t is
        # the same class of failure as an out-of-window decode.
        return None
    return dt if _in_window(dt) else None


def _parse_js_date(value: Any) -> datetime | None:
    """The one ``posts[].created_at`` format the export uses (see _JS_DATE_RE)."""
    from .linkedin_map import _MONTHS

    if not value:
        return None
    text = str(value).strip()
    match = _JS_DATE_RE.match(text)
    if match:
        month = _MONTHS.get(match.group("mon")[:3].lower())
        if month:
            try:
                return datetime(
                    int(match.group("year")),
                    month,
                    int(match.group("day")),
                    int(match.group("h") or 0),
                    int(match.group("m") or 0),
                    int(match.group("s") or 0),
                    tzinfo=UTC,
                )
            except ValueError:
                return None
        return None
    # Belt-and-braces for a future export revision that switches to ISO: cheap
    # to keep, and the plausibility window still gates the result.
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def latest_activity(record: dict) -> tuple[datetime, str] | None:
    """The freshest piece of real activity evidence in a LinkedIn record.

    A two-rung ladder, ordered by measured coverage:

      1. ``activity[].id`` URNs -> ('linkedin_activity'), 48.7% of records;
      2. ``posts[].created_at`` -> ('linkedin_post'), 2.4% of records.

    None means **no activity evidence**, which the caller must write as
    ``activity_at = NULL`` — not as the import clock. Rung 1 is checked in full
    before rung 2 is consulted: a decodable URN is a harder fact than a
    free-text date, and the two are almost never both present.

    ``posts[].id`` carries the same decodable URN (the sample's
    ``6208170746225745920`` decodes to 2016-11-26, matching its own
    ``created_at``), so rung 2 could be widened later; it is left out here so
    the two rungs stay distinguishable in ``activity_kind`` and the 2.4% does
    not silently absorb the post-URN population.
    """
    best: datetime | None = None
    for item in record.get("activity") or []:
        if not isinstance(item, dict):
            continue
        dt = decode_activity_ts(item.get("id"))
        if dt and (best is None or dt > best):
            best = dt
    if best is not None:
        return best, "linkedin_activity"

    for item in record.get("posts") or []:
        if not isinstance(item, dict):
            continue
        dt = _parse_js_date(item.get("created_at"))
        if dt and _in_window(dt) and (best is None or dt > best):
            best = dt
    if best is not None:
        return best, "linkedin_post"
    return None
