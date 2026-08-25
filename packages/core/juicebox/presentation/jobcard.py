"""The one job card — CANDIDATE_AGENT_PLAN §9.

``jobcard()`` takes ``(JobPosting, JobMatch | None, CandidatePreference |
None)`` and returns everything any surface may show: identity, the strongest
reasons drawn from criteria evidence, any veto, a source badge, freshness, and
the single action available. No renderer computes any of that.

**Designed for text first.** ``render_text`` has a hard character budget, and
a field that cannot survive being read in a single text is not a headline
field — the web surfaces add detail, the card does not grow to fit them.

**The candidate never sees a score.** The projection carries reasons and a
recruiter-only score field the candidate serializers must drop; keeping the
number here (flagged) rather than omitting it lets the recruiter renderer be
the same card too.
"""

from __future__ import annotations

from typing import Any

from ..applications.bar import proposal_reasons, veto_reason
from ..models import CandidatePreference, JobMatch, JobPosting

# The text renderer's budget per card line block. prompts.MAX_QUESTION_CHARS
# (320) is the precedent for one message; a card block is one line of a
# multi-card message, so it gets a line budget instead.
MAX_CARD_LINE_CHARS = 90

_SOURCE_LABELS = {
    "project": "mandate",
    "exa": "open market",
    "manual": "added by hand",
    "csv": "imported",
}


def jobcard(
    posting: JobPosting,
    match: JobMatch | None = None,
    preference: CandidatePreference | None = None,
) -> dict[str, Any]:
    """The projection. Plain dict so every surface serializes it as-is.

    ``recruiter_only`` holds what candidate-facing serializers must strip —
    the whitelist tests in the portal enforce that they do.
    """
    criteria = list(match.criteria or []) if match is not None else []
    reasons = proposal_reasons(criteria)
    veto = veto_reason(criteria)

    remote = bool(posting.open_to_remote)
    location = (posting.location or "").strip()
    if remote:
        location = f"{location} (remote ok)".strip() if location else "Remote"

    return {
        "posting_id": posting.id,
        "match_id": match.id if match is not None else "",
        "company": posting.company or "",
        "title": posting.title or "",
        "location": location,
        "seniority": posting.seniority or "",
        "reasons": reasons,
        "veto": veto,
        "source": posting.source,
        "source_label": _SOURCE_LABELS.get(posting.source, posting.source),
        "status": posting.status,
        "url": posting.source_url or "",
        "posted_at": posting.posted_at.isoformat() if posting.posted_at else "",
        "last_seen_at": posting.last_seen_at.isoformat() if posting.last_seen_at else "",
        # The single action a surface offers on this card.
        "action": "apply" if match is not None else "view",
        "recruiter_only": {
            "score": float(match.score or 0.0) if match is not None else None,
            "match_status": match.status if match is not None else "",
            "criteria": criteria,
        },
    }


def render_card(card: dict[str, Any], *, index: int, total: int) -> str:
    """One role, one message -- the shape a phone actually renders.

    A numbered list of three roles in a single bubble is a wall of text you
    scroll past. Sent one at a time each role gets its own bubble, which is
    how every job app on a phone presents them and what the candidate can
    react to individually.

    The position ("2/3") stays because the reply grammar is ordinal: "apply to
    2" has to refer to something the person can see.
    """
    head = f"{index}/{total}  {card['title']} — {card['company']}"
    lines = [head[:MAX_CARD_LINE_CHARS]]
    if card.get("location"):
        lines.append(card["location"][:MAX_CARD_LINE_CHARS])
    for reason in (card.get("reasons") or [])[:2]:
        lines.append(f"· {reason}"[:MAX_CARD_LINE_CHARS])
    if card.get("url"):
        lines.append(card["url"])
    return "\n".join(lines)


def render_text(cards: list[dict[str, Any]], *, footer: str = "") -> str:
    """The thread renderer: numbered lines, one reason each, a footer link.

    The hard budget is what keeps the projection honest — a card that cannot
    say why in one clause here does not deserve a paragraph anywhere else.
    """
    lines: list[str] = []
    for index, card in enumerate(cards, start=1):
        head = f"{index}. {card['title']} — {card['company']}"
        if card.get("location"):
            head += f" — {card['location']}"
        lines.append(head[:MAX_CARD_LINE_CHARS])
        reasons = card.get("reasons") or []
        if reasons:
            lines.append(f"   fits: {reasons[0]}"[:MAX_CARD_LINE_CHARS])
    if cards:
        lines.append("Reply with a number for details, or 'apply to 1'.")
    if footer:
        lines.append(footer)
    return "\n".join(lines)
