"""The query-path funnel. NO external API call happens here on a warm parse
cache: the archetype scores + highlights are precomputed, and query embedding is
a local model.

    1. parse -> structured filter (cached)
    2. SQL filter + ORDER BY archetype score DESC, take top rerank_pool
    3. pgvector rerank on the free-text part, blend
    4. drop below the display floor, return up to `limit` (never padded)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..embed.model import embed_query, to_halfvec_literal
from ..score.archetypes import load_archetypes
from .parse import ParsedFilter, parse_query


def _as_json(value: Any, default):
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


# Bounded to the result set (rerank_pool at most), never the count query. The
# source <> 'guess' predicate lives HERE, in SQL, so a guessed address cannot
# reach any consumer of the search payload.
EMAILS_LATERAL = """
        left join lateral (
            select jsonb_agg(jsonb_build_object(
                     'email', e.email, 'source', e.source, 'is_work', e.is_work,
                     'confidence', e.confidence, 'verified', e.verified)
                   order by e.is_work desc, e.confidence desc, e.first_seen_at) as emails
            from candidate_emails e
            where e.candidate_id = c.id and e.source <> 'guess'
        ) em on true
"""


@dataclass
class SearchResult:
    query: str
    parser: str
    archetype_id: str | None
    archetype_label: str | None
    total_matched: int
    returned: int
    display_floor: int
    message: str | None
    results: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "parser": self.parser,
            "archetype": (
                {"id": self.archetype_id, "label": self.archetype_label}
                if self.archetype_id
                else None
            ),
            "total_matched": self.total_matched,
            "returned": self.returned,
            "display_floor": self.display_floor,
            "message": self.message,
            "results": self.results,
        }


def _build_filters(
    parsed: ParsedFilter, start: int, *, has_contact: bool = False
) -> tuple[str, list, int]:
    clauses = [
        f"s.last_push_at > now() - interval '{int(settings.activity_floor_months)} months'",
        "c.embedding is not null",
    ]
    if has_contact:
        # Parameterless on purpose so the $i arithmetic below is untouched.
        # Guesses are excluded here too: a guess is never "contactable".
        clauses.append(
            "(c.linkedin_url is not null or exists ("
            " select 1 from candidate_emails e"
            " where e.candidate_id = c.id and e.source <> 'guess'))"
        )
    params: list = []
    i = start
    if parsed.metros:
        clauses.append(f"c.location_raw ilike any(${i}::text[])")
        params.append([f"%{m}%" for m in parsed.metros])
        i += 1
    if parsed.languages:
        clauses.append(f"s.primary_language = any(${i}::text[])")
        params.append([lang.lower() for lang in parsed.languages])
        i += 1
    if parsed.experience_min is not None:
        clauses.append(f"s.est_experience_years >= ${i}")
        params.append(float(parsed.experience_min))
        i += 1
    if parsed.experience_max is not None:
        clauses.append(f"s.est_experience_years <= ${i}")
        params.append(float(parsed.experience_max))
        i += 1
    return " and ".join(clauses), params, i


def _payload(row: Any, rank: int, blend: float, has_score: bool) -> dict:
    emails = _as_json(row["emails"], []) if "emails" in row.keys() else []
    emails = emails or []
    return {
        "rank": rank,
        "login": row["login"],
        "name": row["name"],
        "github_url": f"https://github.com/{row['login']}",
        "linkedin_url": row["linkedin_url"],
        "linkedin_source": row["linkedin_source"],
        # Observed addresses only: guesses are filtered in SQL and can never
        # appear here (and therefore never become a send-to address downstream).
        "emails": emails,
        "has_contact": bool(emails) or bool(row["linkedin_url"]),
        "employer": row["employer"],
        "employer_source": row["employer_source"],
        "employer_domain": row["employer_domain"] if "employer_domain" in row.keys() else None,
        "est_experience_years": (
            round(float(row["est_experience_years"]), 1)
            if row["est_experience_years"] is not None
            else None
        ),
        "experience_confidence": row["experience_confidence"],
        "primary_language": row["primary_language"],
        "location": row["location_raw"],
        "top_repos": _as_json(row["top_repos"], []),
        "topics": list(row["topics"])[:12] if "topics" in row.keys() and row["topics"] else [],
        "last_push_at": row["last_push_at"].isoformat() if row["last_push_at"] else None,
        "highlights": _as_json(row["highlights"], []) if has_score else [],
        "flags": _as_json(row["flags"], []) if has_score else [],
        "domain_markers": list(row["domain_markers"]),
        # score is kept in the payload but must never be rendered (see static/index.html).
        "score": row["score"] if has_score else None,
        "blend": round(blend, 4),
        # every inferred field is labeled so a recruiter never mistakes it for fact.
        "inferred": {
            "employer": row["employer"] is not None,
            "est_experience_years": True,
            "linkedin_url": False,
            "emails": False,  # guesses excluded upstream; everything here is observed
        },
    }


async def _vector_fallback(conn, query: str, qvec: str, parser: str, limit: int) -> SearchResult:
    rows = await conn.fetch(
        f"""
        select c.id, c.login, c.name, c.location_raw, c.linkedin_url, c.linkedin_source,
               s.employer, s.employer_source, s.employer_domain, s.primary_language,
               s.est_experience_years, s.experience_confidence, s.last_push_at,
               s.top_repos, s.domain_markers, s.topics,
               null::smallint as score, '[]'::jsonb as highlights, '[]'::jsonb as flags,
               em.emails,
               (1 - (c.embedding <=> $1::halfvec)) as cos_sim
        from candidates c
        join candidate_signals s on s.candidate_id = c.id
        {EMAILS_LATERAL}
        where c.embedding is not null
        order by c.embedding <=> $1::halfvec
        limit $2
        """,
        qvec,
        limit,
    )
    results = [_payload(r, i + 1, float(r["cos_sim"] or 0.0), has_score=False) for i, r in enumerate(rows)]
    return SearchResult(
        query=query,
        parser=parser,
        archetype_id=None,
        archetype_label=None,
        total_matched=len(results),
        returned=len(results),
        display_floor=settings.display_floor,
        message="No archetype matched, so this is a pure semantic search. Name a role "
        "(e.g. 'senior backend', 'ML platform') to get scored, evidence-backed results.",
        results=results,
    )


async def run_search(
    conn,
    query: str,
    limit: int | None = None,
    *,
    use_llm: bool | None = None,
    has_contact: bool = False,
) -> SearchResult:
    limit = limit or settings.result_limit
    parsed, parser = await parse_query(conn, query, use_llm=use_llm)
    qvec = to_halfvec_literal(embed_query(parsed.free_text or query))

    arch = load_archetypes().get(parsed.archetype_id) if parsed.archetype_id else None
    if arch is None:
        return await _vector_fallback(conn, query, qvec, parser, limit)

    # Count query: $1 = archetype, filter params start at $2. has_contact is in
    # the filters so total_matched reflects the real contactable pool.
    where_count, filter_params, _ = _build_filters(parsed, start=2, has_contact=has_contact)
    total_matched = await conn.fetchval(
        f"""
        select count(*) from candidate_signals s
        join candidates c on c.id = s.candidate_id
        join archetype_scores a on a.candidate_id = c.id and a.archetype_id = $1
        where {where_count}
        """,
        arch.id,
        *filter_params,
    )

    # Main query: $1 = archetype, $2 = pool limit, filter params start at $3, qvec last.
    where_main, filter_params, qvec_i = _build_filters(parsed, start=3, has_contact=has_contact)
    rows = await conn.fetch(
        f"""
        with pool as (
            select c.id
            from candidate_signals s
            join candidates c on c.id = s.candidate_id
            join archetype_scores a on a.candidate_id = c.id and a.archetype_id = $1
            where {where_main}
            order by a.score desc
            limit $2
        )
        select c.id, c.login, c.name, c.location_raw, c.linkedin_url, c.linkedin_source,
               s.employer, s.employer_source, s.employer_domain, s.primary_language,
               s.est_experience_years, s.experience_confidence, s.last_push_at,
               s.top_repos, s.domain_markers, s.topics,
               a.score, a.highlights, a.flags,
               em.emails,
               (1 - (c.embedding <=> ${qvec_i}::halfvec)) as cos_sim
        from pool p
        join candidates c on c.id = p.id
        join candidate_signals s on s.candidate_id = c.id
        join archetype_scores a on a.candidate_id = c.id and a.archetype_id = $1
        {EMAILS_LATERAL}
        """,
        arch.id,
        settings.rerank_pool,
        *filter_params,
        qvec,
    )

    blended = []
    for r in rows:
        cos = float(r["cos_sim"] or 0.0)
        final = settings.score_weight * (r["score"] / 100.0) + settings.vector_weight * cos
        blended.append((final, r))
    blended.sort(key=lambda x: x[0], reverse=True)

    above = [(f, r) for f, r in blended if r["score"] >= settings.display_floor]
    chosen = above[:limit]
    results = [_payload(r, i + 1, f, has_score=True) for i, (f, r) in enumerate(chosen)]

    if len(above) < settings.min_results_before_widen:
        message = (
            f"Only {len(above)} candidate(s) cleared the bar for {arch.label}. "
            f"Widen the search (drop a filter or lower the experience floor) to see more."
        )
    else:
        message = f"{len(above)} candidates cleared the bar for {arch.label}."

    return SearchResult(
        query=query,
        parser=parser,
        archetype_id=arch.id,
        archetype_label=arch.label,
        total_matched=total_matched,
        returned=len(results),
        display_floor=settings.display_floor,
        message=message,
        results=results,
    )
