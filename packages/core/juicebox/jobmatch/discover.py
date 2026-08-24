"""Conversational Exa discovery. Transient — nothing is written here."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..llm.client import LLMUnavailable, get_llm
from ..llm.prompts import (
    DISCOVER_PLANNER_SCHEMA,
    DISCOVER_PLANNER_SYSTEM,
    DISCOVER_SUMMARISER_SCHEMA,
    DISCOVER_SUMMARISER_SYSTEM,
    discover_planner_prompt,
    discover_summariser_prompt,
)
from ..models import JobPosting
from ..schemas import DiscoveredPosting, DiscoverStep, DiscoverTurnOut
from .exa import ExaRequestFailed, ExaUnavailable, get_exa
from .ids import source_id_for

logger = logging.getLogger(__name__)

MAX_QUERIES = 3
MAX_NUM_RESULTS = 25


@dataclass
class _Plan:
    action: str
    questions: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    reply: str = ""


def _text(message: Any) -> str:
    if hasattr(message, "content"):
        return str(message.content or "").strip()
    return str((message or {}).get("content") or "").strip()


def _role(message: Any) -> str:
    if hasattr(message, "role"):
        return str(message.role or "")
    return str((message or {}).get("role") or "")


def joined_user_text(messages: list[Any]) -> str:
    """All user turns, in order — a follow-up alone is a bad Exa query."""
    return " ".join(_text(m) for m in messages if _role(m) == "user" and _text(m)).strip()


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _fallback_plan(messages: list[Any]) -> _Plan:
    query = joined_user_text(messages)
    return _Plan(
        action="search",
        queries=[query] if query else [],
        assumptions=["Used your message as the search directly."],
        reply="",
    )


def _plan(messages: list[Any]) -> _Plan:
    llm = get_llm()
    if not llm.available:
        return _fallback_plan(messages)
    try:
        result = llm.json_fast(
            system=DISCOVER_PLANNER_SYSTEM,
            prompt=discover_planner_prompt(messages),
            schema=DISCOVER_PLANNER_SCHEMA,
            effort="low",
            max_tokens=2000,
        )
        data = result.data or {}
    except (LLMUnavailable, Exception) as exc:  # noqa: BLE001 - degrade, never fail the turn
        logger.warning("discover planner failed, using the brief as the query: %s", exc)
        return _fallback_plan(messages)

    action = str(data.get("action") or "search").strip().lower()
    if action not in {"ask", "search"}:
        action = "search"
    questions = _strings(data.get("questions"))
    queries = _strings(data.get("queries"))[:MAX_QUERIES]
    assumptions = _strings(data.get("assumptions"))
    reply = str(data.get("reply") or "").strip()
    if action == "search" and not queries:
        fallback = _fallback_plan(messages)
        queries = fallback.queries
        if not assumptions:
            assumptions = fallback.assumptions
    if action == "ask" and not questions:
        action = "search"
        if not queries:
            queries = _fallback_plan(messages).queries
    return _Plan(
        action=action,
        questions=questions if action == "ask" else [],
        queries=queries if action == "search" else [],
        assumptions=assumptions,
        reply=reply,
    )


def _exa_rows(raw_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_list:
        url = str(raw.get("url") or "").strip()
        title = str(raw.get("title") or "").strip()
        text = str(raw.get("text") or raw.get("summary") or "").strip()
        if not title and not text:
            continue
        # `author` is the page byline, not the employer -- on job boards it is
        # empty, and when it is not it is the board itself. `location` is not a
        # field Exa search returns at all. Both were read here anyway, which is
        # why every ingested posting had a blank company and blank location.
        # Left empty on purpose: `parse.resolve_identity` reads them out of the
        # posting text, where they actually are.
        company = ""
        location = ""
        sid = source_id_for(url=url, title=title, company=company)
        if sid in seen:
            continue
        seen.add(sid)
        rows.append(
            {
                "source_id": sid,
                "title": title or "Untitled role",
                "company": company,
                "location": location,
                "jd_text": text,
                "source_url": url,
            }
        )
    return rows


def _compose_reply(*, n_results: int, n_new: int, n_have: int) -> str:
    noun = "posting" if n_results == 1 else "postings"
    bits = [f"{n_results} {noun} below."]
    if n_have:
        bits.append(f"{n_have} already yours.")
    if n_new and n_have:
        bits.append(f"{n_new} new.")
    return " ".join(bits)


def _summarise(*, n_results: int, n_new: int, n_have: int, queries: list[str]) -> str:
    fallback = _compose_reply(n_results=n_results, n_new=n_new, n_have=n_have)
    llm = get_llm()
    if not llm.available:
        return fallback
    try:
        result = llm.json_fast(
            system=DISCOVER_SUMMARISER_SYSTEM,
            prompt=discover_summariser_prompt(
                n_results=n_results, n_new=n_new, n_have=n_have, queries=queries
            ),
            schema=DISCOVER_SUMMARISER_SCHEMA,
            effort="low",
            max_tokens=400,
        )
        data = result.data or {}
    except (LLMUnavailable, Exception) as exc:  # noqa: BLE001 - degrade
        logger.warning("discover summariser failed: %s", exc)
        return fallback
    reply = str(data.get("reply") or "").strip() or fallback
    follow = str(data.get("follow_up") or "").strip()
    if follow:
        return f"{reply} {follow}".strip()
    return reply


def discover_turn(
    db: Session,
    org_id: str,
    messages: list[Any],
    *,
    num_results: int = 10,
) -> DiscoverTurnOut:
    num_results = max(1, min(int(num_results or 10), MAX_NUM_RESULTS))
    plan = _plan(messages)
    if plan.action == "ask":
        return DiscoverTurnOut(
            reply=plan.reply or "A couple of details before I search.",
            questions=plan.questions,
            assumptions=plan.assumptions,
            steps=[],
            results=[],
        )

    client = get_exa()
    if not client.available:
        return DiscoverTurnOut(
            reply=(
                "Exa is not configured. Set EXA_API_KEY on the API and worker "
                "to search the open market."
            ),
            questions=[],
            assumptions=plan.assumptions,
            steps=[
                DiscoverStep(
                    label="Exa not configured",
                    detail="EXA_API_KEY is not set",
                    count=0,
                )
            ],
            results=[],
        )

    steps: list[DiscoverStep] = []
    raw_hits: list[dict[str, Any]] = []
    # De-duplicated: the planner can propose the same query twice, and each one
    # is a billed search.
    planned: list[str] = []
    for query in plan.queries:
        if query and query not in planned:
            planned.append(query)
    try:
        for query in planned[:MAX_QUERIES]:
            # Per-query, so one bad response does not discard the hits the
            # earlier queries were already billed for.
            try:
                hits = client.search_job_postings(query, num_results=num_results)
            except ExaRequestFailed as exc:
                logger.warning("exa discover search failed for %r: %s", query, exc)
                steps.append(
                    DiscoverStep(label=query, detail="Exa search failed", count=0)
                )
                continue
            steps.append(
                DiscoverStep(label=query, detail="Exa search", count=len(hits))
            )
            raw_hits.extend(hits)
    except ExaUnavailable:
        return DiscoverTurnOut(
            reply=(
                "Exa is not configured. Set EXA_API_KEY on the API and worker "
                "to search the open market."
            ),
            questions=[],
            assumptions=plan.assumptions,
            steps=[
                DiscoverStep(
                    label="Exa not configured",
                    detail="EXA_API_KEY is not set",
                    count=0,
                )
            ],
            results=[],
        )

    rows = _exa_rows(raw_hits)
    ids = [row["source_id"] for row in rows]
    existing: dict[str, str] = {}
    if ids:
        for posting_id, source_id in db.execute(
            select(JobPosting.id, JobPosting.source_id).where(
                JobPosting.org_id == org_id, JobPosting.source_id.in_(ids)
            )
        ):
            existing[source_id] = posting_id
    n_have = sum(1 for row in rows if row["source_id"] in existing)
    n_new = len(rows) - n_have
    steps.append(
        DiscoverStep(
            label="deduped",
            detail=f"{n_new} new, {n_have} already yours",
            count=len(rows),
        )
    )
    results = [
        DiscoveredPosting(
            source_id=row["source_id"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            jd_text=row["jd_text"],
            source_url=row["source_url"],
            already_have=row["source_id"] in existing,
            posting_id=existing.get(row["source_id"]),
        )
        for row in rows
    ]
    reply = _summarise(
        n_results=len(results), n_new=n_new, n_have=n_have, queries=plan.queries
    )
    return DiscoverTurnOut(
        reply=reply or plan.reply,
        questions=[],
        assumptions=plan.assumptions,
        steps=steps,
        results=results,
    )
