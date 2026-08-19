"""Shared scoring helpers: candidate selection, prompt content, defensive JSON
parsing, cost projection, and the offline heuristic fallback.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from .archetypes import Archetype

# Per-call cost, config-driven so the projection and the spend guard track the
# actual FORAY_SCORING_MODEL. Default matches gpt-oss-120b measured spend
# (~$0.0003/call); the old hard-coded value was Haiku-batch pricing, 4.8x high.
from ..config import settings  # noqa: E402

PER_CALL_USD = settings.scoring_usd_per_call

HEDGE_WORDS = {"strong", "excellent", "impressive", "solid", "passionate", "talented"}

SELECT_COLUMNS = """
    c.id, c.login, c.embed_blob,
    s.employer, s.employer_source, s.primary_language, s.language_mix,
    s.total_stars, s.max_repo_stars, s.non_fork_repo_count,
    s.commits_last_year, s.prs_last_year, s.last_push_at,
    s.est_experience_years, s.experience_confidence, s.domain_markers,
    s.oss_maintainer, s.top_repos, s.topics
"""


def _as_json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


async def gate_count(conn, arch: Archetype) -> int:
    return await conn.fetchval(
        f"select count(*) from candidate_signals s "
        f"join candidates c on c.id = s.candidate_id where ({arch.gate})"
    )


async def candidates_to_score(
    conn, arch: Archetype, *, stale_only: bool, limit: int
) -> list[Any]:
    clause = "a.rubric_hash is distinct from $1"
    if stale_only:
        clause += " and a.candidate_id is not null"
    return await conn.fetch(
        f"""
        select {SELECT_COLUMNS}
        from candidate_signals s
        join candidates c on c.id = s.candidate_id
        left join archetype_scores a
               on a.candidate_id = c.id and a.archetype_id = $2
        -- build_user_content falls back to bare login when embed_blob is empty,
        -- producing a garbage score with a MATCHING rubric_hash that is then
        -- never rescored. Never score a candidate before it is embedded.
        where ({arch.gate}) and ({clause}) and c.embed_blob is not null
        order by c.id
        {'limit $3' if limit else ''}
        """,
        arch.rubric_hash,
        arch.id,
        *([limit] if limit else []),
    )


def _mix_summary(language_mix: dict[str, float]) -> str:
    top = list(language_mix.items())[:3]
    return ", ".join(f"{lang} {int(frac * 100)}%" for lang, frac in top) or "n/a"


def build_user_content(row: Any) -> str:
    language_mix = _as_json(row["language_mix"], {})
    last_push = row["last_push_at"]
    last_push_str = last_push.strftime("%Y-%m") if last_push else "unknown"
    return (
        f"{row['embed_blob'] or row['login']}\n\n"
        f"Signals: ~{row['est_experience_years']}y experience "
        f"({row['experience_confidence']} confidence); "
        f"primary language {row['primary_language'] or 'unknown'}; "
        f"language mix {_mix_summary(language_mix)}; "
        f"{row['total_stars']} total stars, top repo {row['max_repo_stars']} stars; "
        f"{row['commits_last_year']} commits and {row['prs_last_year']} PRs last year; "
        f"last active {last_push_str}; "
        f"employer {row['employer'] or 'unknown'} ({row['employer_source'] or 'n/a'}); "
        f"domain markers {list(row['domain_markers'])}."
    )


def parse_scoring_json(text: str) -> dict[str, Any]:
    """Strip fences, json.loads, coerce fields. Raises on anything unparseable."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else body
        body = body.rsplit("```", 1)[0]
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        # Models sometimes wrap the object in prose; take the outermost braces.
        start, end = body.find("{"), body.rfind("}")
        if start == -1 or end <= start:
            raise
        obj = json.loads(body[start : end + 1])
    score = int(obj["score"])
    score = max(0, min(100, score))
    highlights = [str(h) for h in (obj.get("highlights") or [])]
    rationale = str(obj.get("rationale") or "")
    flags = [str(f) for f in (obj.get("flags") or [])]
    return {"score": score, "highlights": highlights, "rationale": rationale, "flags": flags}


# ----------------------------------------------------------- offline heuristic --
# Explicit, spend-free scorer for demos/tests (model = 'offline-heuristic'). It is
# NOT silent degradation: it is only used when explicitly requested with --offline.


def _months_since(dt: datetime | None) -> float:
    if not dt:
        return 9999.0
    return (datetime.now(timezone.utc) - dt).days / 30.44


def heuristic_score(row: Any, arch: Archetype) -> int:
    """A spread-out, spend-free proxy for the real LLM score (demos/tests only)."""
    markers = set(row["domain_markers"])
    score = 20.0
    if arch.gate_markers() & markers:
        score += 20
    score += min(15.0, float(row["est_experience_years"]))
    score += min(20.0, math.log10(max(1, row["total_stars"])) * 7)
    if row["oss_maintainer"]:
        score += 10
    if row["commits_last_year"] > 300:
        score += 5
    elif row["commits_last_year"] < 100:
        score -= 5
    months = _months_since(row["last_push_at"])
    if months > 18:
        score -= 20
    elif months > 12:
        score -= 8
    if row["non_fork_repo_count"] < 2 or row["total_stars"] < 10:
        score -= 15
    return max(0, min(100, round(score)))


def heuristic_result(row: Any, arch: Archetype) -> dict[str, Any]:
    top_repos = _as_json(row["top_repos"], [])
    highlights: list[str] = []
    if top_repos and top_repos[0].get("stars", 0) >= 100:
        top = top_repos[0]
        highlights.append(f"Maintains {top['name']}, {top['stars']} stars")
    if row["employer"]:
        highlights.append(f"{row['employer']} ({row['employer_source']})")
    if row["primary_language"]:
        highlights.append(
            f"{row['primary_language'].title()} across {row['non_fork_repo_count']} repos"
        )
    if row["commits_last_year"] >= 200:
        highlights.append(f"{row['commits_last_year']} commits last year")
    if len(highlights) < 2:
        highlights.append(
            f"{row['non_fork_repo_count']} repos, {row['total_stars']} total stars, no scale evidence"
        )

    flags: list[str] = []
    if _months_since(row["last_push_at"]) > 18:
        flags.append("stale_activity")
    if row["non_fork_repo_count"] < 2 or row["total_stars"] < 10:
        flags.append("thin_evidence")

    return {
        "score": heuristic_score(row, arch),
        "highlights": highlights[:4],
        "rationale": f"offline heuristic for {arch.id}: markers {list(row['domain_markers'])}",
        "flags": flags,
    }
