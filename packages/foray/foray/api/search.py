"""The query-path funnel. NO external API call happens here on a warm parse
cache: the archetype scores + highlights are precomputed, and query embedding is
a local model.

    1. parse -> structured filter (cached)
    2. SQL filter + display floor, take a bounded archetype-score pool
    3. pgvector similarity + a deterministic blend in ``foray.rank``
    4. page the blended pool, then hydrate only the requested rows
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..embed.model import embed_query, to_halfvec_literal
from ..llm import Chat
from ..rank import (
    BlendWeights,
    Page,
    PoolRow,
    RankPolicy,
    Window,
    clamp_window,
    page_presorted,
    rank_page,
    widen_message,
)
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
    """The `/search` wire contract.

    ``total_matched`` counts rows that pass the structured filters *and* clear
    the display floor. It used to ignore the floor, so it advertised people the
    UI would never render; the honest number reads lower for the same query.
    ``paginable`` is the subset actually reachable by ``offset``, capped by
    ``FORAY_MAX_PAGINABLE``, and ``truncated`` says the two disagree. ``rank``
    inside ``results`` is absolute across pages, so page 2 starts at 41.
    """

    query: str
    parser: str
    archetype_id: str | None
    archetype_label: str | None
    total_matched: int
    returned: int
    display_floor: int
    message: str | None
    limit: int
    offset: int
    page: int
    page_count: int
    page_size: int
    paginable: int
    has_more: bool
    truncated: bool
    max_paginable: int
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
            "limit": self.limit,
            "offset": self.offset,
            "page": self.page,
            "page_count": self.page_count,
            "page_size": self.page_size,
            "paginable": self.paginable,
            "has_more": self.has_more,
            "truncated": self.truncated,
            "max_paginable": self.max_paginable,
            "results": self.results,
        }


def _policy() -> RankPolicy:
    return RankPolicy(
        weights=BlendWeights(
            score_weight=settings.score_weight,
            vector_weight=settings.vector_weight,
        ),
        display_floor=settings.display_floor,
        page_size=settings.result_limit,
        max_paginable=settings.max_paginable,
        retrieval_pool=settings.rerank_pool,
        min_results_before_widen=settings.min_results_before_widen,
    )


def _result(
    *,
    query: str,
    parser: str,
    archetype_id: str | None,
    archetype_label: str | None,
    page: Page,
    message: str | None,
    results: list[dict],
    policy: RankPolicy,
) -> SearchResult:
    return SearchResult(
        query=query,
        parser=parser,
        archetype_id=archetype_id,
        archetype_label=archetype_label,
        total_matched=page.total_matched,
        returned=len(results),
        display_floor=policy.display_floor,
        message=message,
        limit=page.limit,
        offset=page.offset,
        page=page.page,
        page_count=page.page_count,
        # The client must page in the server's stride, not a hardcoded one, or
        # FORAY_RESULT_LIMIT would desynchronise the pager from the rows.
        page_size=policy.page_size,
        paginable=page.paginable,
        has_more=page.has_more,
        truncated=page.truncated,
        max_paginable=policy.max_paginable,
        results=results,
    )


def _build_filters(
    parsed: ParsedFilter, start: int, *, has_contact: bool = False
) -> tuple[str, list, int]:
    clauses = [
        f"s.activity_at > now() - interval '{int(settings.activity_floor_months)} months'",
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
    emails = _as_json(row["emails"], []) if "emails" in row else []
    emails = emails or []
    source = row.get("source", "github")
    return {
        "rank": rank,
        "login": row["login"],
        "name": row["name"],
        # A LinkedIn-only person has a synthetic 'li:' login; a github.com link
        # built from it would be a dead 404.
        "source": source,
        "github_url": (
            f"https://github.com/{row['login']}" if source != "linkedin" else None
        ),
        "linkedin_url": row["linkedin_url"],
        "linkedin_source": row["linkedin_source"],
        # Observed addresses only: guesses are filtered in SQL and can never
        # appear here (and therefore never become a send-to address downstream).
        "emails": emails,
        "has_contact": bool(emails) or bool(row["linkedin_url"]),
        "employer": row["employer"],
        "employer_source": row["employer_source"],
        "employer_domain": row.get("employer_domain"),
        "est_experience_years": (
            round(float(row["est_experience_years"]), 1)
            if row["est_experience_years"] is not None
            else None
        ),
        "experience_confidence": row["experience_confidence"],
        "primary_language": row["primary_language"],
        "location": row["location_raw"],
        "top_repos": _as_json(row["top_repos"], []),
        "topics": list(row["topics"])[:12] if row.get("topics") else [],
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


async def _vector_fallback(
    conn,
    query: str,
    qvec: str,
    parser: str,
    parsed: ParsedFilter,
    window: Window,
    policy: RankPolicy,
    *,
    has_contact: bool,
) -> SearchResult:
    where_count, count_params, _ = _build_filters(parsed, start=1, has_contact=has_contact)
    total_matched = int(
        await conn.fetchval(
            f"""
            select count(*)
            from candidates c
            join candidate_signals s on s.candidate_id = c.id
            where c.merged_into is null and {where_count}
            """,
            *count_params,
        )
        or 0
    )

    rows = []
    if window.limit:
        # The vector distance remains the leading ORDER BY expression so a
        # pgvector KNN index can serve it. Candidate id makes exact distance
        # ties deterministic for offset pagination; retain it even if a future
        # EXPLAIN shows an incremental sort above the index scan.
        where_main, filter_params, _ = _build_filters(
            parsed, start=4, has_contact=has_contact
        )
        rows = await conn.fetch(
            f"""
            select c.id, c.login, c.name, c.source, c.location_raw,
                   c.linkedin_url, c.linkedin_source,
                   s.employer, s.employer_source, s.employer_domain, s.primary_language,
                   s.est_experience_years, s.experience_confidence, s.last_push_at,
                   s.top_repos, s.domain_markers, s.topics,
                   null::smallint as score, '[]'::jsonb as highlights,
                   '[]'::jsonb as flags, em.emails,
                   (1 - (c.embedding <=> $1::halfvec)) as cos_sim
            from candidates c
            join candidate_signals s on s.candidate_id = c.id
            {EMAILS_LATERAL}
            where c.merged_into is null and {where_main}
            order by c.embedding <=> $1::halfvec, c.id
            limit $2 offset $3
            """,
            qvec,
            window.limit,
            window.offset,
            *filter_params,
        )

    page = page_presorted(
        [
            PoolRow(key=int(row["id"]), score=None, cos_sim=float(row["cos_sim"] or 0.0))
            for row in rows
        ],
        total_matched=total_matched,
        window=window,
        policy=policy,
    )
    ranked_by_id = {item.key: item for item in page.items}
    results = [
        _payload(
            row,
            ranked_by_id[int(row["id"])].rank,
            ranked_by_id[int(row["id"])].blend,
            has_score=False,
        )
        for row in rows
    ]
    return _result(
        query=query,
        parser=parser,
        archetype_id=None,
        archetype_label=None,
        page=page,
        message="No archetype matched, so this is a pure semantic search. Name a role "
        "(e.g. 'senior backend', 'ML platform') to get scored, evidence-backed results.",
        results=results,
        policy=policy,
    )


async def run_search(
    conn,
    query: str,
    limit: int | None = None,
    *,
    offset: int = 0,
    use_llm: bool | None = None,
    has_contact: bool = False,
    chat: Chat | None = None,
) -> SearchResult:
    policy = _policy()
    window = clamp_window(offset, limit or 0, policy)
    parsed, parser = await parse_query(conn, query, use_llm=use_llm, chat=chat)
    qvec = to_halfvec_literal(embed_query(parsed.free_text or query))

    arch = load_archetypes().get(parsed.archetype_id) if parsed.archetype_id else None
    if arch is None:
        return await _vector_fallback(
            conn,
            query,
            qvec,
            parser,
            parsed,
            window,
            policy,
            has_contact=has_contact,
        )

    # Count query: $1 = archetype, $2 = floor, filter params start at $3.
    # total_matched is the honest eligible set: filters + display floor.
    where_count, filter_params, _ = _build_filters(parsed, start=3, has_contact=has_contact)
    total_matched = int(
        await conn.fetchval(
        f"""
        select count(*) from candidate_signals s
        join candidates c on c.id = s.candidate_id
        join archetype_scores a on a.candidate_id = c.id and a.archetype_id = $1
        where c.merged_into is null and a.score >= $2::smallint and {where_count}
        """,
        arch.id,
        policy.display_floor,
        *filter_params,
        )
        or 0
    )

    # Narrow rank query: no lateral email aggregation or JSON payload hydration.
    # The retrieval cutoff is by archetype score; rank.py documents that the
    # final blend is therefore an approximation within this bounded pool.
    where_main, filter_params, qvec_i = _build_filters(
        parsed, start=4, has_contact=has_contact
    )
    rows = await conn.fetch(
        f"""
        with pool as (
            select c.id, a.score
            from candidate_signals s
            join candidates c on c.id = s.candidate_id
            join archetype_scores a on a.candidate_id = c.id and a.archetype_id = $1
            where c.merged_into is null and a.score >= $2::smallint and {where_main}
            order by a.score desc, a.candidate_id
            limit $3
        )
        select p.id, p.score,
               (1 - (c.embedding <=> ${qvec_i}::halfvec)) as cos_sim
        from pool p
        join candidates c on c.id = p.id
        order by p.score desc, p.id
        """,
        arch.id,
        policy.display_floor,
        policy.pool_size,
        *filter_params,
        qvec,
    )

    page = rank_page(
        [
            PoolRow(
                key=int(row["id"]),
                score=int(row["score"]),
                cos_sim=float(row["cos_sim"] or 0.0),
            )
            for row in rows
        ],
        total_matched=total_matched,
        window=window,
        policy=policy,
    )

    hydrated = []
    if page.items:
        hydrated = await conn.fetch(
            f"""
            select c.id, c.login, c.name, c.source, c.location_raw,
                   c.linkedin_url, c.linkedin_source,
                   s.employer, s.employer_source, s.employer_domain, s.primary_language,
                   s.est_experience_years, s.experience_confidence, s.last_push_at,
                   s.top_repos, s.domain_markers, s.topics,
                   a.score, a.highlights, a.flags, em.emails
            from candidates c
            join candidate_signals s on s.candidate_id = c.id
            join archetype_scores a on a.candidate_id = c.id and a.archetype_id = $2
            {EMAILS_LATERAL}
            where c.id = any($1::bigint[])
            """,
            [item.key for item in page.items],
            arch.id,
        )

    hydrated_by_id = {int(row["id"]): row for row in hydrated}
    results = [
        _payload(hydrated_by_id[item.key], item.rank, item.blend, has_score=True)
        for item in page.items
        if item.key in hydrated_by_id
    ]
    message = widen_message(
        total_matched=page.total_matched,
        paginable=page.paginable,
        archetype_label=arch.label,
        policy=policy,
    )

    return _result(
        query=query,
        parser=parser,
        archetype_id=arch.id,
        archetype_label=arch.label,
        page=page,
        message=message,
        results=results,
        policy=policy,
    )
