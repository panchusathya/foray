"""Is this person on the market?

The answer lives in ``CandidatePreference`` -- what somebody said about their
own situation on an intake call -- and it was invisible to search. A recruiter
pasting a JD got the best profile match in the database whether or not that
person had just signed a new contract.

Two rules shape everything here.

**Silence is not a "no".** Most of the database has never been asked. An absent
preference record is ``unknown``, which scores as ``unknown`` -- a criterion
the UI renders and the weighted mean skips -- never as a miss. Hard-filtering
on availability by default would hide everybody who has simply never had an
intake call.

**A statement decays.** "I'm actively looking" is a fact about the week it was
said. At six months it is a lead, at a year it is history, so the market state
is graded against ``captured_at`` rather than trusted forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Ordered most to least available. The order is the tie-break the UI sorts on.
MARKET_STATES = (
    "active",  # said they are searching
    "open",  # employed, would move for the right thing
    "passive",  # a stale signal, or engaged but never asked directly
    "not_looking",  # said no, or was just placed
    "unknown",  # never asked
)

# employment_status (intake.schema.EMPLOYMENT_STATUSES) -> market state.
_STATUS_STATE = {
    "unemployed_searching": "active",
    "employed_searching": "active",
    "employed_open": "open",
    "student": "open",
    "employed_not_looking": "not_looking",
}

# How urgently they said they would move (intake.schema.TIMELINES).
_TIMELINE_RANK = {
    "immediate": 1.0,
    "under_1_month": 0.9,
    "1_3_months": 0.75,
    "3_6_months": 0.5,
    "exploratory": 0.3,
}

_TIMELINE_LABEL = {
    "immediate": "can start immediately",
    "under_1_month": "within a month",
    "1_3_months": "in 1-3 months",
    "3_6_months": "in 3-6 months",
    "exploratory": "exploring only",
}

_STATE_LABEL = {
    "active": "Actively looking",
    "open": "Open to a move",
    "passive": "Possibly open",
    "not_looking": "Not looking",
    "unknown": "Not asked",
}

# A preference stays current for a quarter, is worth less for a year, and after
# that says nothing about today.
FRESH_DAYS = 90
STALE_DAYS = 365

# CRM facts that override anything said on a call.
_CRM_NOT_LOOKING = {"placed", "do_not_contact"}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass
class Availability:
    """A person's market state, as search sees it."""

    state: str = "unknown"
    employment_status: str = ""
    timeline: str = ""
    captured_at: datetime | None = None
    days_since: int | None = None
    target_base: float | None = None
    target_total: float | None = None
    comp_currency: str = ""
    active_processes: int = 0
    offers_in_hand: int = 0
    willing_to_relocate: bool | None = None
    work_authorization: str = ""
    reason_for_looking: str = ""
    crm_status: str = ""
    confirmed: bool = False

    @property
    def known(self) -> bool:
        return self.state != "unknown"

    @property
    def stale(self) -> bool:
        return self.days_since is not None and self.days_since > FRESH_DAYS

    @property
    def label(self) -> str:
        return _STATE_LABEL.get(self.state, _STATE_LABEL["unknown"])

    @property
    def detail(self) -> str:
        """One line a recruiter can act on without opening the profile."""
        if self.state == "unknown":
            return "No intake call on file"
        bits: list[str] = []
        if self.timeline:
            bits.append(_TIMELINE_LABEL.get(self.timeline, self.timeline))
        if self.offers_in_hand:
            plural = "" if self.offers_in_hand == 1 else "s"
            bits.append(f"{self.offers_in_hand} offer{plural} in hand")
        elif self.active_processes:
            noun = "process" if self.active_processes == 1 else "processes"
            bits.append(f"{self.active_processes} {noun} live")
        if self.days_since is not None:
            if self.days_since <= 1:
                bits.append("asked today")
            elif self.days_since < FRESH_DAYS:
                bits.append(f"asked {self.days_since}d ago")
            else:
                bits.append(f"last asked {self.days_since}d ago")
        return " · ".join(bits)

    def score(self) -> float:
        """0..1 availability, decayed by how long ago the person said it."""
        base = {
            "active": 1.0,
            "open": 0.7,
            "passive": 0.45,
            "not_looking": 0.0,
            "unknown": 0.0,
        }[self.state]
        if self.state in {"not_looking", "unknown"}:
            return base
        if self.timeline:
            # Urgency moves the score within the band the state sets, so a
            # "searching, but exploratory" never outranks "open, can start now".
            base = 0.6 * base + 0.4 * _TIMELINE_RANK.get(self.timeline, 0.5)
        if self.days_since is None:
            return base
        if self.days_since <= FRESH_DAYS:
            return base
        if self.days_since >= STALE_DAYS:
            return base * 0.35
        span = STALE_DAYS - FRESH_DAYS
        return base * (1.0 - 0.65 * ((self.days_since - FRESH_DAYS) / span))

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "label": self.label,
            "detail": self.detail,
            "score": round(self.score(), 4),
            "employment_status": self.employment_status,
            "timeline": self.timeline,
            "days_since": self.days_since,
            "stale": self.stale,
            "target_base": self.target_base,
            "target_total": self.target_total,
            "comp_currency": self.comp_currency,
            "active_processes": self.active_processes,
            "offers_in_hand": self.offers_in_hand,
            "willing_to_relocate": self.willing_to_relocate,
            "work_authorization": self.work_authorization,
            "reason_for_looking": self.reason_for_looking,
            "crm_status": self.crm_status,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> Availability:
        data = payload or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def from_preference(preference, *, crm_status: str = "", now: datetime | None = None) -> Availability:
    """Read one ``CandidatePreference`` row into a market state.

    ``preference`` may be ``None``: most of the corpus has never been asked,
    and that has to come back ``unknown`` rather than as a low score.
    """
    now = now or datetime.now(UTC)
    crm_status = crm_status or ""

    if preference is None:
        # A placement is a fact about the relationship, not a claim on a call,
        # so it still answers the question when nobody has been interviewed.
        state = "not_looking" if crm_status in _CRM_NOT_LOOKING else "unknown"
        return Availability(state=state, crm_status=crm_status)

    captured = _utc(preference.captured_at) or _utc(preference.updated_at)
    days_since = max(0, (now - captured).days) if captured else None

    status = (preference.employment_status or "").strip()
    state = _STATUS_STATE.get(status, "")
    if not state:
        # A preference record with no employment status still means somebody
        # sat through an intake call about moving jobs.
        state = "passive" if _has_signal(preference) else "unknown"

    # Placed or do-not-contact outranks whatever the call captured: both are
    # set deliberately, and later.
    if crm_status in _CRM_NOT_LOOKING:
        state = "not_looking"
    elif state in {"active", "open"} and days_since is not None and days_since > STALE_DAYS:
        # Past a year, "actively looking" is not a claim about today. Kept as
        # passive rather than dropped -- they wanted to move once.
        state = "passive"

    return Availability(
        state=state,
        employment_status=status,
        timeline=(preference.timeline or "").strip(),
        captured_at=captured,
        days_since=days_since,
        target_base=preference.target_base,
        target_total=preference.target_total,
        comp_currency=(preference.comp_currency or "").strip(),
        active_processes=len(preference.active_processes or []),
        offers_in_hand=len(preference.offers_in_hand or []),
        willing_to_relocate=preference.willing_to_relocate,
        work_authorization=(preference.work_authorization or "").strip(),
        reason_for_looking=(preference.reason_for_looking or "").strip(),
        crm_status=crm_status,
        confirmed=preference.confirmed_at is not None,
    )


def _has_signal(preference) -> bool:
    return any(
        (
            preference.timeline,
            preference.target_base,
            preference.target_total,
            preference.active_processes,
            preference.offers_in_hand,
            preference.reason_for_looking,
        )
    )
