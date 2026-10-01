"""Sending hours: the part of the day candidate email may go out.

An org setting, off by default. When on, nothing is sent to a candidate
outside [start, end) on the chosen weekdays in the chosen timezone (Pacific
by default); sends that fall due outside it wait, keeping their place in
line, until the window next opens. Notes a mailbox sends to its own owner
(draft ready, follow-up reminders) are not candidate email and ignore it.

An end before the start means the window runs past midnight (22:00-02:00);
the weekday is the day the window opens. Breaks (12:00-13:00, say) are
carved out of every day's window: nothing goes during one, and what was due
waits for the break to end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "America/Los_Angeles"
DEFAULT_START = "09:00"
DEFAULT_END = "17:00"
WEEKDAYS = [0, 1, 2, 3, 4]  # Monday..Friday


@dataclass(frozen=True)
class SendWindow:
    enabled: bool
    start: time
    end: time
    tz: ZoneInfo
    days: frozenset[int]
    breaks: tuple[tuple[time, time], ...] = ()


def _parse(value: str | None, fallback: str) -> time:
    try:
        hours, minutes = (value or fallback).split(":")[:2]
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        hours, minutes = fallback.split(":")
        return time(int(hours), int(minutes))


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def _breaks(raw) -> tuple[tuple[time, time], ...]:
    out = []
    for item in raw or []:
        try:
            start = _parse(item.get("start"), "")
            end = _parse(item.get("end"), "")
        except (AttributeError, ValueError):
            continue
        if start < end:
            out.append((start, end))
    return tuple(sorted(out))


def org_window(org) -> SendWindow:
    days = getattr(org, "send_window_days", None)
    return SendWindow(
        enabled=bool(getattr(org, "send_window_enabled", False)),
        start=_parse(getattr(org, "send_window_start", None), DEFAULT_START),
        end=_parse(getattr(org, "send_window_end", None), DEFAULT_END),
        tz=_zone(getattr(org, "send_window_tz", None)),
        days=frozenset(days if days is not None else WEEKDAYS),
        breaks=_breaks(getattr(org, "send_window_breaks", None)),
    )


def _spans(window: SendWindow, local_day: datetime) -> tuple[datetime, datetime]:
    """The window that opens on this local calendar day."""
    opens = local_day.replace(
        hour=window.start.hour, minute=window.start.minute, second=0, microsecond=0
    )
    closes = local_day.replace(
        hour=window.end.hour, minute=window.end.minute, second=0, microsecond=0
    )
    if closes <= opens:  # runs past midnight
        closes += timedelta(days=1)
    return opens, closes


def _open_intervals(window: SendWindow, local_day: datetime) -> list[tuple[datetime, datetime]]:
    """That day's window with the breaks taken out, as (opens, closes) pairs."""
    opens, closes = _spans(window, local_day)
    pieces = [(opens, closes)]
    for b_start, b_end in window.breaks:
        cut_from = opens.replace(hour=b_start.hour, minute=b_start.minute)
        cut_to = opens.replace(hour=b_end.hour, minute=b_end.minute)
        if cut_from < opens:  # an overnight window's break after midnight
            cut_from += timedelta(days=1)
            cut_to += timedelta(days=1)
        next_pieces = []
        for a, b in pieces:
            if cut_to <= a or cut_from >= b:
                next_pieces.append((a, b))
                continue
            if a < cut_from:
                next_pieces.append((a, cut_from))
            if cut_to < b:
                next_pieces.append((cut_to, b))
        pieces = next_pieces
    return pieces


def _intervals_around(window: SendWindow, local: datetime):
    # Yesterday first: an overnight window that opened yesterday may still be open.
    for offset in range(-1, 8):
        day = (local + timedelta(days=offset)).replace(tzinfo=None).replace(tzinfo=window.tz)
        if day.weekday() not in window.days:
            continue
        yield from _open_intervals(window, day)


def is_open(window: SendWindow, now: datetime) -> bool:
    return next_open(window, now) <= now


def next_open(window: SendWindow, now: datetime) -> datetime:
    """``now`` if candidate email may go now, else when it next may."""
    if not window.enabled or not window.days:
        return now
    local = now.astimezone(window.tz)
    for opens, closes in _intervals_around(window, local):
        if closes <= local:
            continue
        if opens <= local:
            return now
        return opens.astimezone(UTC)
    return now  # nothing open in the next week (all breaks): do not hold forever


def window_wait(window: SendWindow, now: datetime) -> datetime | None:
    """When sending may resume, if it may not now."""
    opens = next_open(window, now)
    return opens if opens > now else None


def closes_at(window: SendWindow, now: datetime) -> datetime | None:
    """When the open stretch we are in ends: the window's close or a break."""
    if not window.enabled or not is_open(window, now):
        return None
    local = now.astimezone(window.tz)
    for opens, closes in _intervals_around(window, local):
        if opens <= local < closes:
            return closes.astimezone(UTC)
    return None
