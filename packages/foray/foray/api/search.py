"""The query-path funnel. NO external API call happens here on a warm parse
cache: the archetype scores + highlights are precomputed, query embedding is
a local model, and the optional cross-encoder is local too.

    1. parse -> structured filter (cached). Writes; stays outside the snapshot.
    2. one read-only repeatable-read transaction:
         Q1 counts, Q2 vector arm, Q3 lexical arm, optional score arm, Q4 enrich
    3. RRF fusion + 3-term blend in ``foray.rank``; optional local CE on the head
    4. page the fused pool, then hydrate only the requested rows

``parse_query`` writes to ``query_parse_cache``, so it MUST run before the
read-only transaction. That ordering is load-bearing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from ..config import settings
from ..embed.model import embed_query, to_halfvec_literal
from ..evidence import Evidence, build_evidence
from ..llm import Chat
from ..log import get_logger
from ..rank import (
    BlendWeights,
    Page,
    PoolRow,
    Ranked,
    RankPolicy,
    apply_ce_scores,
    blend_one,
    build_pool,
    clamp_window,
    needs_rerank,
    rank_page,
    sort_key,
    widen_message,
)
from ..score.archetypes import load_archetypes
from ..search_doc import RANK_NORMALIZATION, RANK_WEIGHTS, TS_CONFIG, build_websearch_or
from .parse import ParsedFilter, parse_query

log = get_logger("api.search")

EmbedFn = Callable[[str], Any]


# Bounded to the result set, never the count query. The source <> 'guess'
# predicate lives HERE, in SQL, so a guessed address cannot reach any consumer
# of the search payload.
EMAILS_LATERAL = """
        left join lateral (
            select jsonb_agg(jsonb_build_object(
                     'email', e.email, 'source', e.source, 'is_work', e.is_work,
                     'confidence', e.confidence, 'verified', e.verified,
                     'reach_score', e.reach_score)
                   -- Personal first, deliberately. The consumer of emails[0]
                   -- is outreach: forayImport flattens this list in order, the
                   -- main app's Candidate.primary_email is emails[0], and
                   -- send_step sends there. A recruiting pitch must not land in
                   -- somebody's work inbox, so a work address is the fallback,
                   -- never the default. Confidence still orders within each
                   -- group.
                   order by e.is_work asc, e.confidence desc, e.first_seen_at) as emails
            from candidate_emails e
            where e.candidate_id = c.id and e.source <> 'guess'
        ) em on true
"""

_ALLOWED_ARMS = frozenset({"vector", "lexical", "score"})
_pgvector_version: str | None = None
_pgvector_probed = False


@dataclass
class Params:
    """Positional-parameter accumulator. Numbering derives from insertion order."""

    values: list = field(default_factory=list)

    def add(self, value: Any) -> str:
        self.values.append(value)
        return f"${len(self.values)}"


def parse_arms(raw: str | None = None) -> tuple[str, ...]:
    text = (raw if raw is not None else settings.retrieval_arms) or "vector,lexical"
    parts = tuple(dict.fromkeys(p.strip().lower() for p in text.split(",") if p.strip()))
    unknown = [p for p in parts if p not in _ALLOWED_ARMS]
    if unknown:
        log.warning("search.unknown_arms", unknown=unknown)
    kept = tuple(p for p in parts if p in _ALLOWED_ARMS)
    return kept or ("vector", "lexical")


@dataclass
class SearchResult:
    """The `/search` wire contract.

    ``total_matched`` counts rows that pass the structured filters AND, when
    an archetype matched, its gate (the retrievable universe). ``paginable`` is the fused pool after the floor /
    min-evidence vetoes, capped by ``FORAY_MAX_PAGINABLE``. ``rank`` inside
    ``results`` is absolute across pages, so page 2 starts at 41.
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
    min_evidence: int = 2
    reranked: bool = False
    rerank_depth: int = 0
    rerank_model: str | None = None
    rerank_reason: str = "disabled"
    lexical_indexed: int = 0
    pgvector_version: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
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
            "min_evidence": self.min_evidence,
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
            "reranked": self.reranked,
            "rerank_depth": self.rerank_depth,
            "rerank_model": self.rerank_model,
            "rerank_reason": self.rerank_reason,
            "lexical_indexed": self.lexical_indexed,
            "pgvector_version": self.pgvector_version,
            "diagnostics": self.diagnostics,
            "results": self.results,
        }


def _policy(*, n_arms: int = 2) -> RankPolicy:
    return RankPolicy(
        weights=BlendWeights(
            rrf_weight=settings.w_rrf,
            score_weight=settings.w_score,
            vector_weight=settings.w_vector,
        ),
        display_floor=settings.display_floor,
        page_size=settings.result_limit,
        max_paginable=settings.max_paginable,
        min_results_before_widen=settings.min_results_before_widen,
        unscored_score=settings.unscored_score,
        min_evidence=settings.min_evidence,
        rerank_depth=settings.rerank_depth,
        ce_floor=settings.rerank_ce_floor,
        rrf_k=settings.rrf_k,
        n_arms=max(1, n_arms),
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
    reranked: bool = False,
    rerank_reason: str = "disabled",
    lexical_indexed: int = 0,
    pgvector_version: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> SearchResult:
    return SearchResult(
        query=query,
        parser=parser,
        archetype_id=archetype_id,
        archetype_label=archetype_label,
        total_matched=page.total_matched,
        returned=len(results),
        display_floor=policy.display_floor,
        min_evidence=policy.min_evidence,
        message=message,
        limit=page.limit,
        offset=page.offset,
        page=page.page,
        page_count=page.page_count,
        page_size=policy.page_size,
        paginable=page.paginable,
        has_more=page.has_more,
        truncated=page.truncated,
        max_paginable=policy.max_paginable,
        reranked=reranked,
        rerank_depth=policy.rerank_depth,
        rerank_model=settings.rerank_model if settings.rerank_enabled else None,
        rerank_reason=rerank_reason,
        lexical_indexed=lexical_indexed,
        pgvector_version=pgvector_version,
        diagnostics=diagnostics or {},
        results=results,
    )


def _filter_clauses(
    parsed: ParsedFilter,
    params: Params,
    *,
    has_contact: bool = False,
    gate_sql: str | None = None,
) -> str:
    clauses = [
        "c.merged_into is null",
        f"s.activity_at > now() - interval '{int(settings.activity_floor_months)} months'",
        "c.embedding is not null",
    ]
    if has_contact:
        clauses.append(
            "(c.linkedin_url is not null or exists ("
            " select 1 from candidate_emails e"
            " where e.candidate_id = c.id and e.source <> 'guess'))"
        )
    if parsed.metros:
        placeholder = params.add([f"%{m}%" for m in parsed.metros])
        clauses.append(f"c.location_raw ilike any({placeholder}::text[])")
    if parsed.languages:
        placeholder = params.add([lang.lower() for lang in parsed.languages])
        clauses.append(f"s.primary_language = any({placeholder}::text[])")
    if parsed.experience_min is not None:
        placeholder = params.add(float(parsed.experience_min))
        clauses.append(f"s.est_experience_years >= {placeholder}")
    if parsed.experience_max is not None:
        placeholder = params.add(float(parsed.experience_max))
        clauses.append(f"s.est_experience_years <= {placeholder}")
    if gate_sql:
        # The archetype gate is the relevance predicate: without it any Bay
        # Area person with the right experience band is retrievable for an ML
        # search, because the gate previously only shaped offline scoring
        # (score/). Interpolated as literal SQL, not a bind parameter, on
        # purpose: the gate is repo-controlled (archetypes.yaml), contains no
        # $N placeholders, is whitespace-normalised at load, and is validated
        # with EXPLAIN at sync time (score/archetypes.validate_gates) — the
        # same practice as score/common.py. It must NEVER carry user input;
        # user-derived values go through ``params`` above. Its now() is safe
        # under the repeatable-read snapshot.
        clauses.append(f"({gate_sql})")
    return " and ".join(clauses)


def _payload(
    row: Any,
    ranked: Ranked,
    evidence: Evidence,
    *,
    has_score: bool,
) -> dict:
    emails = _as_json(row["emails"], []) if "emails" in row else []
    emails = emails or []
    source = row.get("source", "github")
    score = row["score"] if has_score else None
    return {
        "rank": ranked.rank,
        "login": row["login"],
        "name": row["name"],
        "source": source,
        "github_url": (
            f"https://github.com/{row['login']}" if source != "linkedin" else None
        ),
        "linkedin_url": row["linkedin_url"],
        "linkedin_source": row["linkedin_source"],
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
        "highlights": evidence.highlights,
        "flags": evidence.flags,
        "highlight_source": evidence.highlight_source,
        "evidence": {
            "highlights": evidence.highlights,
            "flags": evidence.flags,
            "highlight_source": evidence.highlight_source,
        },
        "domain_markers": list(row["domain_markers"] or []),
        # score is kept in the payload but must never be rendered.
        "score": int(score) if score is not None else None,
        "blend": round(ranked.blend, 4),
        "band": ranked.band,
        "inferred": {
            "employer": row["employer"] is not None,
            "est_experience_years": True,
            "linkedin_url": False,
            "emails": False,
        },
    }


def _as_json(value: Any, default):
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


async def probe_pgvector_version(conn) -> str | None:
    global _pgvector_version, _pgvector_probed
    if _pgvector_probed:
        return _pgvector_version
    row = await conn.fetchrow("select extversion from pg_extension where extname = 'vector'")
    _pgvector_version = row["extversion"] if row else None
    _pgvector_probed = True
    return _pgvector_version


@asynccontextmanager
async def _snapshot(conn):
    """Repeatable-read snapshot when we own the connection; a passthrough when
    the caller already has a transaction open (tests). Isolation cannot change
    mid-transaction, so SET LOCAL still applies to the outer transaction."""
    if conn.is_in_transaction():
        yield
        return
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        yield


async def _ensure_pgvector_loaded(conn) -> None:
    """Load pgvector's .so so ``hnsw.*`` GUCs exist in this session.

    Those GUCs are registered when the library loads, not at connect.
    ``set_config('hnsw.ef_search', ...)`` before that creates a dummy custom
    GUC that HNSW never reads — and ``SHOW`` raises UndefinedObjectError.
    Touching a vector type forces the load.
    """
    await conn.execute("select null::vector")


async def apply_hnsw_gucs(conn, vector_k: int) -> str | None:
    """SET LOCAL so a pooled connection cannot leak ef_search to the next request."""
    version = await probe_pgvector_version(conn)
    try:
        await _ensure_pgvector_loaded(conn)
    except asyncpg.UndefinedObjectError:
        return version
    try:
        if version and _version_tuple(version) >= (0, 8):
            await conn.execute("select set_config('hnsw.iterative_scan', 'relaxed_order', true)")
            await conn.execute(
                "select set_config('hnsw.ef_search', $1, true)", str(settings.hnsw_ef_search)
            )
        else:
            # Pre-0.8 has no iterative scan; ef_search must cover the arm LIMIT or
            # post-filtering yields a handful of rows regardless of LIMIT.
            ef = max(settings.hnsw_ef_search, int(vector_k * 1.6), 40)
            await conn.execute("select set_config('hnsw.ef_search', $1, true)", str(ef))
    except asyncpg.UndefinedObjectError:
        log.warning("search.hnsw_guc_missing", pgvector_version=version)
    return version


def _build_evidence_for(row: Any, query: str, policy: RankPolicy) -> Evidence:
    score = row.get("score")
    score_int = int(score) if score is not None else None
    return build_evidence(
        row,
        query=query,
        stored_highlights=row.get("highlights"),
        stored_flags=row.get("flags"),
        score=score_int,
        display_floor=policy.display_floor,
        min_evidence=policy.min_evidence,
    )


class _StageClock:
    """Per-stage elapsed ms so a latency regression is attributable."""

    def __init__(self) -> None:
        self.marks: dict[str, float] = {}
        self._t = time.perf_counter()

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.marks[name] = round((now - self._t) * 1000.0, 2)
        self._t = now


async def run_search(
    conn,
    query: str,
    limit: int | None = None,
    *,
    offset: int = 0,
    use_llm: bool | None = None,
    has_contact: bool = False,
    chat: Chat | None = None,
    embed: EmbedFn | None = None,
    rerank_scorer=None,
) -> SearchResult:
    clock = _StageClock()
    arms = parse_arms()
    # parse_query writes to query_parse_cache. It must stay outside the
    # read-only snapshot transaction below.
    parsed, parser = await parse_query(conn, query, use_llm=use_llm, chat=chat)
    clock.mark("parse")
    embed_fn = embed or embed_query
    text = parsed.free_text or query
    qvec = to_halfvec_literal(embed_fn(text))
    tsq = build_websearch_or(text)
    clock.mark("embed")
    arch = load_archetypes().get(parsed.archetype_id) if parsed.archetype_id else None
    if arch is None:
        arms = tuple(a for a in arms if a != "score") or ("vector",)
    live_arms = []
    if "vector" in arms:
        live_arms.append("vector")
    if "lexical" in arms and tsq:
        live_arms.append("lexical")
    if "score" in arms and arch is not None:
        live_arms.append("score")
    if not live_arms:
        live_arms = ["vector"]
    policy = _policy(n_arms=len(live_arms))
    window = clamp_window(offset, limit or 0, policy)

    vector_ids: list[int] = []
    lexical_ids: list[int] = []
    score_ids: list[int] = []
    enrich_rows: list[Any] = []
    n_matched = 0
    n_lexical = 0
    n_scored = 0
    pg_ver: str | None = None

    async with _snapshot(conn):
        pg_ver = await apply_hnsw_gucs(conn, settings.vector_k)
        clock.mark("gucs")
        # When an archetype matched, its gate applies to the count and to all
        # three retrieval arms alike, so total_matched / n_lexical / n_scored
        # describe the GATED universe — the same one the arms draw from. On
        # the score arm the gate is a no-op predicate (scored rows passed it
        # by construction), kept anyway for consistency. arch is None
        # (vector-fallback) leaves behaviour unchanged.
        gate = arch.gate if arch is not None else None
        params = Params()
        where = _filter_clauses(parsed, params, has_contact=has_contact, gate_sql=gate)
        arch_ph = params.add(arch.id) if arch is not None else None
        arch_join = (
            f"left join archetype_scores a on a.candidate_id = c.id "
            f"and a.archetype_id = {arch_ph}"
            if arch_ph
            else "left join archetype_scores a on false"
        )
        count_sql = f"""
            select
              count(*)::int as n_matched,
              count(*) filter (where s.search_tsv is not null)::int as n_lexical_indexed,
              count(*) filter (where a.score is not null)::int as n_scored
            from candidates c
            join candidate_signals s on s.candidate_id = c.id
            {arch_join}
            where {where}
        """
        counts = await conn.fetchrow(count_sql, *params.values)
        n_matched = int(counts["n_matched"] or 0) if counts else 0
        n_lexical = int(counts["n_lexical_indexed"] or 0) if counts else 0
        n_scored = int(counts["n_scored"] or 0) if counts else 0
        clock.mark("count")

        if n_matched and "vector" in live_arms:
            vparams = Params()
            vwhere = _filter_clauses(parsed, vparams, has_contact=has_contact, gate_sql=gate)
            qph = vparams.add(qvec)
            lim = vparams.add(settings.vector_k)
            vector_ids = [
                int(r["id"])
                for r in await conn.fetch(
                    f"""
                    select c.id
                    from candidates c
                    join candidate_signals s on s.candidate_id = c.id
                    where {vwhere}
                    order by c.embedding <=> {qph}::halfvec, c.id
                    limit {lim}
                    """,
                    *vparams.values,
                )
            ]
        clock.mark("vector")

        if n_matched and "lexical" in live_arms and tsq:
            lparams = Params()
            lwhere = _filter_clauses(parsed, lparams, has_contact=has_contact, gate_sql=gate)
            tph = lparams.add(tsq)
            lim = lparams.add(settings.lexical_k)
            lexical_ids = [
                int(r["id"])
                for r in await conn.fetch(
                    f"""
                    select c.id
                    from candidates c
                    join candidate_signals s on s.candidate_id = c.id
                    where {lwhere}
                      and s.search_tsv @@ websearch_to_tsquery('{TS_CONFIG}'::regconfig, {tph})
                    order by ts_rank_cd('{RANK_WEIGHTS}'::real[], s.search_tsv,
                              websearch_to_tsquery('{TS_CONFIG}'::regconfig, {tph}),
                              {RANK_NORMALIZATION}) desc, c.id
                    limit {lim}
                    """,
                    *lparams.values,
                )
            ]
        clock.mark("lexical")

        if n_matched and "score" in live_arms and arch is not None:
            sparams = Params()
            swhere = _filter_clauses(parsed, sparams, has_contact=has_contact, gate_sql=gate)
            aph = sparams.add(arch.id)
            floor_ph = sparams.add(policy.display_floor)
            lim = sparams.add(settings.score_k)
            score_ids = [
                int(r["id"])
                for r in await conn.fetch(
                    f"""
                    select c.id
                    from candidates c
                    join candidate_signals s on s.candidate_id = c.id
                    join archetype_scores a
                      on a.candidate_id = c.id and a.archetype_id = {aph}
                    where {swhere} and a.score >= {floor_ph}::smallint
                    order by a.score desc, c.id
                    limit {lim}
                    """,
                    *sparams.values,
                )
            ]
        clock.mark("score")

        pool_ids = list(dict.fromkeys([*vector_ids, *lexical_ids, *score_ids]))
        if pool_ids:
            eparams = Params()
            ids_ph = eparams.add(pool_ids)
            qph = eparams.add(qvec)
            aph = eparams.add(arch.id) if arch is not None else None
            arch_join = (
                f"left join archetype_scores a on a.candidate_id = c.id "
                f"and a.archetype_id = {aph}"
                if aph
                else "left join archetype_scores a on false"
            )
            enrich_rows = await conn.fetch(
                f"""
                select c.id, c.embed_blob,
                       (1 - (c.embedding <=> {qph}::halfvec)) as cos_sim,
                       a.score, a.highlights, a.flags,
                       s.employer, s.employer_source, s.employer_domain, s.title,
                       s.primary_language, s.non_fork_repo_count, s.total_stars,
                       s.max_repo_stars, s.commits_last_year, s.prs_last_year,
                       s.last_push_at, s.est_experience_years, s.experience_confidence,
                       s.domain_markers, s.topics, s.top_repos, s.evidence_kind,
                       s.tenure_months, s.activity_at
                from candidates c
                join candidate_signals s on s.candidate_id = c.id
                {arch_join}
                where c.id = any({ids_ph}::bigint[])
                """,
                *eparams.values,
            )
        clock.mark("enrich")

    if (
        "lexical" in parse_arms()
        and n_matched
        and n_lexical == 0
        and tsq is not None
    ):
        log.warning("search.lexical_arm_empty", matched=n_matched)

    evidence_by_id: dict[int, Evidence] = {}
    enrich_by_id: dict[int, PoolRow] = {}
    raw_by_id: dict[int, Any] = {}
    for row in enrich_rows:
        cid = int(row["id"])
        raw_by_id[cid] = row
        ev = _build_evidence_for(row, text, policy)
        evidence_by_id[cid] = ev
        score = int(row["score"]) if row["score"] is not None else None
        enrich_by_id[cid] = PoolRow(
            key=cid,
            score=score,
            cos_sim=float(row["cos_sim"] or 0.0),
            # PoolRow.highlight_count feeds the min_evidence veto, so it now
            # carries the SUBSTANTIVE evidence count (highlights minus the
            # honesty filler evidence.py pads the display list with). Counting
            # rendered sentences made the veto a near no-op: the pads
            # guaranteed every row reached 2. The wire payload still renders
            # ev.highlights unchanged, filler included.
            highlight_count=ev.evidence_count,
        )

    pool = build_pool(
        vector_ids=vector_ids,
        lexical_ids=lexical_ids,
        score_ids=score_ids,
        enrich=enrich_by_id,
        rrf_k=policy.rrf_k,
    )

    rerank_reason = "disabled"
    reranked = False
    if settings.rerank_enabled and needs_rerank(window, policy) and pool:
        from ..rerank.document import build_document
        from ..rerank.runner import rerank as run_rerank

        retrieval_head = sorted(
            pool,
            key=lambda row: sort_key(
                blend_one(
                    row.score,
                    row.cos_sim,
                    policy.weights,
                    rrf=row.rrf,
                    unscored_score=policy.unscored_score,
                    rrf_k=policy.rrf_k,
                    n_arms=policy.n_arms,
                ),
                row.key,
                band=1,
                cos_sim=row.cos_sim,
            ),
        )[: policy.rerank_depth]
        documents = {
            row.key: build_document(
                raw_by_id[row.key], max_chars=settings.rerank_doc_chars
            )
            for row in retrieval_head
            if row.key in raw_by_id
        }
        outcome = await run_rerank(
            text, documents, scorer=rerank_scorer, ids=list(documents)
        )
        rerank_reason = outcome.reason
        if outcome.reason == "ok" and outcome.logits:
            pool = apply_ce_scores(pool, outcome.logits, policy=policy)
            reranked = True
    elif settings.rerank_enabled and not needs_rerank(window, policy):
        rerank_reason = "below_head"

    page = rank_page(
        pool, total_matched=n_matched, window=window, policy=policy
    )
    clock.mark("rank")

    hydrated: list[Any] = []
    if page.items:
        hydrated = await conn.fetch(
            f"""
            select c.id, c.login, c.name, c.source, c.location_raw,
                   c.linkedin_url, c.linkedin_source,
                   s.employer, s.employer_source, s.employer_domain, s.primary_language,
                   s.est_experience_years, s.experience_confidence, s.last_push_at,
                   s.top_repos, s.domain_markers, s.topics, s.evidence_kind,
                   s.title, s.tenure_months,
                   a.score, a.highlights, a.flags, em.emails
            from candidates c
            join candidate_signals s on s.candidate_id = c.id
            left join archetype_scores a
                   on a.candidate_id = c.id
                  and ($2::text is not null and a.archetype_id = $2)
            {EMAILS_LATERAL}
            where c.id = any($1::bigint[])
            """,
            [item.key for item in page.items],
            arch.id if arch is not None else None,
        )

    hydrated_by_id = {int(row["id"]): row for row in hydrated}
    results = []
    for item in page.items:
        row = hydrated_by_id.get(item.key)
        if row is None:
            continue
        ev = evidence_by_id.get(item.key) or _build_evidence_for(row, text, policy)
        has_score = row["score"] is not None
        results.append(_payload(row, item, ev, has_score=has_score))
    clock.mark("hydrate")

    vector_set = set(vector_ids)
    lexical_set = set(lexical_ids)
    top_keys = [item.key for item in page.items[:10]]
    lex_only = lexical_set - vector_set
    diagnostics = {
        "vector_hits": len(vector_ids),
        "lexical_hits": len(lexical_ids),
        "overlap": len(vector_set & lexical_set),
        "empty_lexical_query": tsq is None,
        "rrf_only_from_lexical_at_10": sum(1 for key in top_keys if key in lex_only),
        "unscored_in_top_10": sum(1 for hit in results[:10] if hit.get("score") is None),
        # Head rows the CE demoted below every unreranked row (band 2).
        # 0 when the request was not reranked.
        "ce_rejected": sum(1 for row in pool if row.band == 2),
        "timings_ms": clock.marks,
        "arms": list(live_arms),
    }

    message = widen_message(
        total_matched=page.total_matched,
        paginable=page.paginable,
        archetype_label=arch.label if arch is not None else None,
        policy=policy,
        scored_matched=n_scored,
        fused=len(pool),
    )
    if arch is None and page.total_matched:
        message = (
            "No archetype matched, so this is hybrid semantic + keyword search. "
            "Name a role (e.g. 'senior backend', 'ML platform') to use precomputed "
            "archetype scores as a ranking signal. " + (message or "")
        )

    return _result(
        query=query,
        parser=parser,
        archetype_id=arch.id if arch is not None else None,
        archetype_label=arch.label if arch is not None else None,
        page=page,
        message=message.strip() or None,
        results=results,
        policy=policy,
        reranked=reranked,
        rerank_reason=rerank_reason,
        lexical_indexed=n_lexical,
        pgvector_version=pg_ver,
        diagnostics=diagnostics,
    )
