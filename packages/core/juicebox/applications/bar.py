"""What "meets our bar" means — CANDIDATE_AGENT_PLAN §5, as a filter.

Not a second scoring system. ``score_pair`` already emits a score plus
per-criterion evidence; the bar is four checks over its output:

1. **A score floor** (``settings.proposal_score_floor``). Necessary, never
   sufficient.
2. **Vetoes are absolute.** A veto criterion is a no, not a low score, and no
   vector term outweighs it.
3. **Explainability is a gate, not a garnish.** We propose only what one plain
   sentence of criteria evidence explains. A score carried by vector
   similarity with no structured criterion behind it is not proposed — "the
   embedding liked it" is not an answer we can give a candidate who asks
   "why this one?".
4. **Freshness.** Proposing a closed or stale role costs more trust than
   proposing nothing.

The candidate never sees a score. Reasons only; the number stays
recruiter-side (§5, and the INTAKE_COMPLIANCE.md §5 boundary).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..config import get_settings
from ..models import JobMatch, JobPosting, utcnow

# How stale an open-market posting may be and still be proposed. Project and
# manual postings have no scraper refreshing last_seen_at, so they are exempt:
# a recruiter closes those by hand.
FRESH_DAYS = 21
_SCRAPED_SOURCES = {"exa"}

# A criterion at or above this is strong enough to carry a reason sentence.
_REASON_FLOOR = 0.8
MAX_REASONS = 3


@dataclass(frozen=True)
class BarDecision:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    why_not: str = ""


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def proposal_reasons(criteria: list[dict] | None) -> list[str]:
    """The strongest evidence sentences, best first, at most ``MAX_REASONS``.

    Evidence text when the criterion carries some; the label as a fallback so
    a strong preference criterion ("Preferred titles") still reads as a
    reason. Never the score.
    """
    scored: list[tuple[float, str]] = []
    for item in criteria or []:
        if item.get("veto"):
            continue
        score = float(item.get("score") or 0.0)
        if score < _REASON_FLOOR:
            continue
        text = (item.get("evidence") or "").strip() or (item.get("label") or "").strip()
        if text:
            scored.append((score, text))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    seen: set[str] = set()
    reasons: list[str] = []
    for _, text in scored:
        if text in seen:
            continue
        seen.add(text)
        reasons.append(text)
        if len(reasons) >= MAX_REASONS:
            break
    return reasons


def veto_reason(criteria: list[dict] | None) -> str:
    for item in criteria or []:
        if item.get("veto"):
            return (item.get("evidence") or item.get("label") or "vetoed").strip()
    return ""


def is_fresh(posting: JobPosting, *, now: datetime | None = None) -> bool:
    if posting.status != "open":
        return False
    if posting.source not in _SCRAPED_SOURCES:
        return True
    seen = _aware(posting.last_seen_at) or _aware(posting.first_seen_at)
    if seen is None:
        return False
    return (now or utcnow()) - seen <= timedelta(days=FRESH_DAYS)


def floor_for(org_id: str, candidate_id: str = "") -> float:
    """The score floor this match is held to.

    One global floor plus two exempt lists. The floor doubles as the kill
    switch for candidate-facing proposals, so without an exemption there is no
    way to test the loop end to end without opening it for everybody.

    The candidate list is the one that matters in practice: the shared intake
    number belongs to the same org as every real candidate, so an org-level
    exemption would open proposals to anyone who texts it. Naming candidates
    opens it for exactly those people and nobody else.
    """
    settings = get_settings()
    if candidate_id and candidate_id in settings.proposal_floor_exempt_candidates:
        return settings.proposal_score_floor_exempt
    if org_id and org_id in settings.proposal_floor_exempt_orgs:
        return settings.proposal_score_floor_exempt
    return settings.proposal_score_floor


def passes_bar(
    match: JobMatch, posting: JobPosting, *, now: datetime | None = None
) -> BarDecision:
    now = now or utcnow()

    if not is_fresh(posting, now=now):
        return BarDecision(False, why_not="stale-or-closed")

    veto = veto_reason(match.criteria)
    if veto:
        return BarDecision(False, why_not=f"veto: {veto}")

    floor = floor_for(
        getattr(match, "org_id", "") or "", getattr(match, "candidate_id", "") or ""
    )
    if float(match.score or 0.0) < floor:
        return BarDecision(False, why_not="below-floor")

    reasons = proposal_reasons(match.criteria)
    if not reasons:
        return BarDecision(False, why_not="unexplainable")

    return BarDecision(True, reasons=reasons)
