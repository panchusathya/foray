"""Deterministic evidence phrases for the Foray result card.

Highlights are the only thing a recruiter sees, so their quality is the
product (archetypes.yaml). This module is the query-path writer: it turns
``candidate_signals`` into checkable phrases, validates stored archetype
highlights at read time, and never emits hedge words.

Every template is <=11 words, names checkable evidence, has no hedge word,
and says nothing about the person. LinkedIn-only rows take a hard branch —
no repo/star/commit/PR phrase is ever emitted for them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

HEDGE_WORDS = {"strong", "excellent", "impressive", "solid", "passionate", "talented"}
MAX_WORDS = 11
_WORD = re.compile(r"[a-z0-9+#./-]+")


def _as_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _months_since(dt: datetime | None) -> float:
    if not dt:
        return 9999.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).days / 30.44


def _word_count(phrase: str) -> int:
    return len(phrase.split())


def validate(phrase: str) -> bool:
    """Drop anything that would violate the archetype highlight contract."""
    if not phrase or not str(phrase).strip():
        return False
    text = str(phrase).strip()
    if _word_count(text) > MAX_WORDS:
        return False
    tokens = {part.lower() for part in text.split()}
    return not tokens & HEDGE_WORDS


def query_terms(query: str) -> list[str]:
    """Lowercased content tokens used to promote query-matched phrases."""
    return [tok for tok in _WORD.findall((query or "").lower()) if len(tok) > 1]


def _matches(phrase: str, terms: Iterable[str]) -> bool:
    hay = phrase.lower()
    return any(term in hay for term in terms)


def _fmt_stars(n: int) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(n)


def _tenure_phrase(months: int | None) -> str | None:
    if not months or months <= 0:
        return None
    years, rem = divmod(int(months), 12)
    if years and rem:
        return f"{years}y {rem}m"
    if years:
        return f"{years}y"
    return f"{rem}m"


def templates_from_signals(row: Mapping[str, Any], *, query: str = "") -> list[str]:
    """Build checkable phrases from signals. LinkedIn is a hard branch."""
    kind = row.get("evidence_kind") or "github"
    terms = query_terms(query)
    phrases = _linkedin_templates(row) if kind == "linkedin" else _github_templates(row)
    phrases = [p for p in phrases if validate(p)]
    if terms:
        phrases.sort(key=lambda phrase: (0 if _matches(phrase, terms) else 1))
    return phrases


def _github_templates(row: Mapping[str, Any]) -> list[str]:
    phrases: list[str] = []
    top_repos = _as_json(row.get("top_repos"), []) or []
    if top_repos:
        top = top_repos[0]
        name = str(top.get("name") or "").strip()
        stars = int(top.get("stars") or 0)
        if name and stars >= 100:
            phrases.append(f"{name}, {_fmt_stars(stars)} stars")
    language = row.get("primary_language")
    non_fork = int(row.get("non_fork_repo_count") or 0)
    if language:
        phrases.append(f"{str(language).title()} across {non_fork} non-fork repos")
    employer = row.get("employer")
    tenure = _tenure_phrase(row.get("tenure_months"))
    if employer and tenure:
        phrases.append(f"{employer} for {tenure}")
    elif employer:
        phrases.append(f"{employer} ({row.get('employer_source') or 'inferred'})")
    commits = int(row.get("commits_last_year") or 0)
    prs = int(row.get("prs_last_year") or 0)
    if commits >= 200:
        phrases.append(f"{commits} commits, {prs} PRs last year")
    topics = list(row.get("topics") or [])[:3]
    if topics:
        phrases.append("Repo topics: " + ", ".join(str(t) for t in topics))
    months = _months_since(row.get("last_push_at") or row.get("activity_at"))
    if months > 18 and months < 9000:
        phrases.append(f"No public push in {int(months)} months")
    total_stars = int(row.get("total_stars") or 0)
    if len(phrases) < 2:
        phrases.append(f"{non_fork} repos, {total_stars} total stars, no scale evidence")
    return phrases


def _linkedin_templates(row: Mapping[str, Any]) -> list[str]:
    phrases: list[str] = []
    title = (row.get("title") or "").strip()
    employer = (row.get("employer") or "").strip()
    tenure = _tenure_phrase(row.get("tenure_months"))
    if title and employer and tenure:
        phrases.append(f"{title} at {employer}, {tenure}")
    elif title and employer:
        phrases.append(f"{title} at {employer}")
    elif employer and tenure:
        phrases.append(f"{employer} for {tenure}")
    elif title:
        phrases.append(title[:80])
    years = row.get("est_experience_years")
    confidence = row.get("experience_confidence") or "low"
    if years:
        phrases.append(f"{int(float(years))}y stated experience ({confidence} confidence)")
    markers = [str(m) for m in (row.get("domain_markers") or []) if m][:4]
    if markers:
        phrases.append("Stated domains: " + ", ".join(markers))
    if len(phrases) < 2:
        phrases.append("LinkedIn profile, no public code evidence")
    return phrases


def flags_from_signals(row: Mapping[str, Any], *, scored: bool, clears_floor: bool) -> list[str]:
    flags: list[str] = []
    kind = row.get("evidence_kind") or "github"
    if kind == "linkedin":
        flags.append("linkedin_only")
    else:
        if _months_since(row.get("last_push_at")) > 18:
            flags.append("stale_activity")
        if int(row.get("non_fork_repo_count") or 0) < 2 or int(row.get("total_stars") or 0) < 10:
            flags.append("thin_evidence")
    if not scored:
        flags.append("unscored")
    elif not clears_floor:
        flags.append("below_archetype_floor")
    return flags


@dataclass(frozen=True)
class Evidence:
    highlights: list[str]
    flags: list[str]
    highlight_source: str
    highlight_count: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "highlight_count", len(self.highlights))


def build_evidence(
    row: Mapping[str, Any],
    *,
    query: str,
    stored_highlights: Any = None,
    stored_flags: Any = None,
    score: int | None,
    display_floor: int,
    min_evidence: int = 2,
) -> Evidence:
    """Compose the phrases a recruiter will see.

    Archetype highlights win only when the parse picked an archetype, the row
    has a score for it, and it clears the floor. Even then a query-matched
    template is appended if the stored set contains none. Stored highlights
    that fail ``validate()`` are dropped at read time.
    """
    stored = [str(h) for h in (_as_json(stored_highlights, []) or []) if validate(str(h))]
    templates = templates_from_signals(row, query=query)
    terms = query_terms(query)
    scored = score is not None
    clears_floor = scored and score >= display_floor  # type: ignore[operator]

    source = "signals"
    highlights: list[str] = []
    if scored and clears_floor and stored:
        highlights = list(stored[:4])
        source = "archetype"
        if terms and not any(_matches(h, terms) for h in highlights):
            for phrase in templates:
                if _matches(phrase, terms) and phrase not in highlights:
                    highlights.append(phrase)
                    source = "mixed"
                    break
        if len(highlights) < min_evidence:
            for phrase in templates:
                if phrase not in highlights:
                    highlights.append(phrase)
                    source = "mixed"
                if len(highlights) >= min_evidence:
                    break
    else:
        highlights = list(templates[:4])
        source = "signals"

    highlights = [h for h in highlights if validate(h)][:4]
    flags = flags_from_signals(row, scored=scored, clears_floor=bool(clears_floor))
    extra = [str(f) for f in (_as_json(stored_flags, []) or []) if f]
    for flag in extra:
        if flag not in flags:
            flags.append(flag)
    return Evidence(highlights=highlights, flags=flags, highlight_source=source)
