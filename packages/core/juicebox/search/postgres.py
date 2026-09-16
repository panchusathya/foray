"""Postgres-backed hybrid retrieval: FTS (tsvector) + pgvector, fused with RRF.

Hard filters run in SQL so we never pull the corpus into RAM. Only the
shortlist is hydrated into ``CandidateDoc``s for scoring and rerank.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Float, String, bindparam, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, TEXT
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..enrich import taxonomy as tx
from ..llm.embeddings import get_embedder, tokenize
from ..models import Candidate, CandidatePreference
from . import fusion
from .availability import from_preference
from .filters import YEARS_MAX_TOLERANCE, YEARS_MIN_TOLERANCE
from .index import CandidateDoc, SearchPool, candidate_to_doc

# to_tsquery() wants an operator between every lexeme; a bare space is a syntax
# error, not an implicit AND. Negative keywords are phrases ("in the us"), so a
# multi-word term went to Postgres verbatim and 500'd the whole search --
# invisible in development, where SQLite uses the in-process backend.
_TSQUERY_OPERATORS = re.compile(r"[&|!()<>:*'\"]+")

# CRM states that answer "are they on the market" regardless of what was said
# on a call. Mirrors ``availability._CRM_NOT_LOOKING``.
_CRM_OVERRIDE = ("placed", "do_not_contact")


def _as_tsquery(tokens: list[str]) -> str | None:
    """OR-query over terms. A multi-word term becomes a phrase match.

    ``<->`` rather than ``&`` because the in-process filter tests a negative
    keyword with ``" term " in haystack`` -- contiguous words, in order. An AND
    would reject anyone whose profile happens to contain both "us" and "the".
    """
    parts: list[str] = []
    for token in tokens:
        words = _TSQUERY_OPERATORS.sub("", token or "").split()
        if not words:
            continue
        parts.append(words[0] if len(words) == 1 else "(" + " <-> ".join(words) + ")")
    if not parts:
        return None
    return " | ".join(parts)


def _vector_literal(values: Any) -> str:
    return "[" + ",".join(f"{float(x):.8g}" for x in values) + "]"


class PostgresIndex:
    """Per-request view of one org's corpus. Holds no in-process postings."""

    backend = "postgres"

    def __init__(self, db: Session, org_id: str) -> None:
        self.db = db
        self.org_id = org_id

    def __len__(self) -> int:
        return int(
            self.db.scalar(select(func.count()).where(Candidate.org_id == self.org_id)) or 0
        )

    def stats(self) -> dict[str, Any]:
        embedder = get_embedder()
        dim = self.db.execute(
            text(
                """
                SELECT vector_dims(embedding)
                FROM candidates
                WHERE org_id = :org AND embedding IS NOT NULL
                LIMIT 1
                """
            ),
            {"org": self.org_id},
        ).scalar()
        return {
            "documents": len(self),
            "vocabulary": None,
            "embedding_dim": int(dim or embedder.dim),
            "backend": self.backend,
        }

    @property
    def docs(self) -> list[CandidateDoc]:
        """Load every profile. Eval and tests use this; search does not."""
        return _hydrate(self.db, self.org_id)

    def search_pool(self, parsed, pool_size: int, *, only_available: bool = False) -> SearchPool:
        eligible_ids, excluded, total_matched = self._eligible(parsed)
        if only_available and eligible_ids:
            eligible_ids, dropped = self._on_market(eligible_ids)
            if dropped:
                excluded["not_on_market"] = dropped
            total_matched = len(eligible_ids)
        if total_matched == 0:
            return SearchPool(
                docs=[],
                lexical=[],
                vector=[],
                fusion=[],
                total_matched=0,
                excluded=excluded,
                backend=self.backend,
            )

        retrieval_text = parsed.retrieval_text()
        tokens = tokenize(retrieval_text)
        lexical_ranks, lexical_scores = self._lexical(eligible_ids, tokens, pool_size)
        vector_ranks, vector_scores = self._vector(eligible_ids, retrieval_text, pool_size)

        settings = get_settings()
        fused = fusion.reciprocal_rank_fusion(
            [lexical_ranks, vector_ranks],
            weights=[1.0, 1.0],
            k=settings.rrf_k,
        )
        # Eligible people with no retrieval overlap still deserve a floor, the
        # same way the in-process index ranks them after the keyword hits.
        for candidate_id in eligible_ids:
            fused.setdefault(candidate_id, 0.0)

        normalized = fusion.normalize_scores(fused)
        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[: max(pool_size, 0)]
        ids = [candidate_id for candidate_id, _ in ordered]
        docs_by_id = {doc.id: doc for doc in _hydrate(self.db, self.org_id, ids=ids)}

        docs: list[CandidateDoc] = []
        lexical: list[float] = []
        vector: list[float] = []
        fusion_scores: list[float] = []
        for candidate_id, _score in ordered:
            doc = docs_by_id.get(candidate_id)
            if doc is None:
                continue
            docs.append(doc)
            lexical.append(float(lexical_scores.get(candidate_id, 0.0)))
            vector.append(float(vector_scores.get(candidate_id, 0.0)))
            fusion_scores.append(float(normalized.get(candidate_id, 0.0)))

        return SearchPool(
            docs=docs,
            lexical=lexical,
            vector=vector,
            fusion=fusion_scores,
            total_matched=total_matched,
            excluded=excluded,
            backend=self.backend,
        )

    def _on_market(self, eligible_ids: list[str]) -> tuple[list[str], int]:
        """Keep only the people who said they would move.

        Deliberately Python rather than another arm of the ``_eligible`` SQL.
        Availability is a small decision tree over employment status, stated
        timeline, CRM state and how long ago the person was asked; writing it
        twice is how the two backends start disagreeing about who is on the
        market. ``candidate_preferences`` holds one row per person who has
        actually been interviewed, so this reads a fraction of the corpus.
        """
        from .filters import AVAILABLE_STATES

        preferences = {
            row.candidate_id: row
            for row in self.db.scalars(
                select(CandidatePreference).where(
                    CandidatePreference.org_id == self.org_id,
                    CandidatePreference.candidate_id.in_(eligible_ids),
                )
            )
        }
        # Only the two statuses that override a stated preference are worth
        # fetching; everything else leaves the reading unchanged.
        overridden = {
            row[0]
            for row in self.db.execute(
                select(Candidate.id).where(
                    Candidate.org_id == self.org_id,
                    Candidate.id.in_(eligible_ids),
                    Candidate.crm_status.in_(tuple(_CRM_OVERRIDE)),
                )
            ).all()
        }
        kept = [
            candidate_id
            for candidate_id in eligible_ids
            if from_preference(
                preferences.get(candidate_id),
                crm_status="placed" if candidate_id in overridden else "",
            ).state
            in AVAILABLE_STATES
        ]
        return kept, len(eligible_ids) - len(kept)

    def _eligible(self, parsed) -> tuple[list[str], dict[str, int], int]:
        """Apply hard filters in SQL. Order matches ``filters.apply_filters``."""
        excluded_companies = {tx.normalize_company(c) for c in parsed.excluded_companies if c}
        excluded_companies.discard("")
        excluded_titles = [tx.normalize_title(t) for t in parsed.title_exclusions if t]
        negative = [k for k in parsed.negative_keywords if k]
        min_years = parsed.min_years_experience
        max_years = parsed.max_years_experience

        params: dict[str, Any] = {
            "org_id": self.org_id,
            "min_bound": (min_years * YEARS_MIN_TOLERANCE) if min_years is not None else None,
            "max_bound": (max_years + YEARS_MAX_TOLERANCE) if max_years is not None else None,
            "excl_companies": list(excluded_companies) or None,
            "excl_titles": excluded_titles or None,
            "neg_tsquery": _as_tsquery(negative),
        }

        # Both sides taxonomy-normalised, via the derived columns a flush
        # listener stamps. Comparing normalised exclusions against the raw
        # display text let "Acme Inc." survive an exclusion of "Acme" -- the
        # in-process filter normalises both sides and did not.
        sql = (
            text(
                """
                WITH params AS (
                    SELECT
                        CAST(:org_id AS text) AS org_id,
                        :excl_companies AS excl_companies,
                        :excl_titles AS excl_titles,
                        CAST(:neg_tsquery AS text) AS neg_tsquery,
                        CAST(:min_bound AS double precision) AS min_bound,
                        CAST(:max_bound AS double precision) AS max_bound
                ),
                base AS (
                    SELECT
                        c.id,
                        (
                            p.excl_companies IS NOT NULL
                            AND (
                                c.current_company_normalized = ANY(p.excl_companies)
                                OR EXISTS (
                                    SELECT 1 FROM experiences e
                                    WHERE e.candidate_id = c.id
                                      AND e.company_normalized = ANY(p.excl_companies)
                                )
                            )
                        ) AS company_hit,
                        (
                            p.excl_titles IS NOT NULL
                            AND c.current_title_normalized = ANY(p.excl_titles)
                        ) AS title_hit,
                        (
                            p.neg_tsquery IS NOT NULL
                            AND c.search_tsv @@ to_tsquery('simple', p.neg_tsquery)
                        ) AS negative_hit,
                        (
                            p.min_bound IS NOT NULL
                            AND coalesce(c.years_experience, 0) < p.min_bound
                        ) AS below_min,
                        (
                            p.max_bound IS NOT NULL
                            AND coalesce(c.years_experience, 0) > p.max_bound
                        ) AS above_max
                    FROM candidates c
                    CROSS JOIN params p
                    WHERE c.org_id = p.org_id
                ),
                classified AS (
                    SELECT
                        id,
                        company_hit,
                        (NOT company_hit AND title_hit) AS title_excl,
                        (NOT company_hit AND NOT title_hit AND negative_hit) AS negative_excl,
                        (NOT company_hit AND NOT title_hit AND NOT negative_hit AND below_min)
                            AS years_min_excl,
                        (NOT company_hit AND NOT title_hit AND NOT negative_hit AND NOT below_min
                            AND above_max) AS years_max_excl
                    FROM base
                )
                SELECT
                    coalesce(
                        array_agg(id ORDER BY id) FILTER (
                            WHERE NOT company_hit AND NOT title_excl AND NOT negative_excl
                              AND NOT years_min_excl AND NOT years_max_excl
                        ),
                        ARRAY[]::text[]
                    ) AS eligible,
                    count(*) FILTER (WHERE company_hit) AS excluded_company,
                    count(*) FILTER (WHERE title_excl) AS excluded_title,
                    count(*) FILTER (WHERE negative_excl) AS negative_keyword,
                    count(*) FILTER (WHERE years_min_excl) AS below_min_years,
                    count(*) FILTER (WHERE years_max_excl) AS above_max_years
                FROM classified
                """
            ).bindparams(
                bindparam("org_id", type_=String()),
                bindparam("excl_companies", type_=ARRAY(TEXT())),
                bindparam("excl_titles", type_=ARRAY(TEXT())),
                bindparam("neg_tsquery", type_=String()),
                bindparam("min_bound", type_=Float()),
                bindparam("max_bound", type_=Float()),
            )
        )
        row = self.db.execute(sql, params).one()
        eligible = list(row.eligible or [])
        excluded: dict[str, int] = {}
        for key, value in (
            ("excluded_company", row.excluded_company),
            ("excluded_title", row.excluded_title),
            ("negative_keyword", row.negative_keyword),
            ("below_min_years", row.below_min_years),
            ("above_max_years", row.above_max_years),
        ):
            if value:
                excluded[key] = int(value)
        return eligible, excluded, len(eligible)

    def _lexical(
        self, eligible_ids: list[str], tokens: list[str], pool_size: int
    ) -> tuple[dict[str, int], dict[str, float]]:
        tsquery = _as_tsquery(tokens)
        if not tsquery or not eligible_ids:
            return {}, {}
        sql = text(
            """
            SELECT c.id AS id, ts_rank_cd(c.search_tsv, to_tsquery('simple', :q)) AS score
            FROM candidates c
            WHERE c.id = ANY(:ids)
              AND c.search_tsv @@ to_tsquery('simple', :q)
            ORDER BY score DESC, c.id
            LIMIT :k
            """
        ).bindparams(
            bindparam("q"),
            bindparam("ids", type_=ARRAY(TEXT())),
            bindparam("k"),
        )
        rows = self.db.execute(
            sql, {"q": tsquery, "ids": eligible_ids, "k": pool_size}
        ).all()
        ranks = {row.id: rank for rank, row in enumerate(rows, start=1)}
        scores = {row.id: float(row.score) for row in rows}
        return ranks, scores

    def _vector(
        self, eligible_ids: list[str], retrieval_text: str, pool_size: int
    ) -> tuple[dict[str, int], dict[str, float]]:
        if not retrieval_text or not eligible_ids:
            return {}, {}
        embedder = get_embedder()
        query_vector = embedder.embed([retrieval_text], input_type="query")[0]
        if query_vector is None or getattr(query_vector, "size", 1) == 0:
            return {}, {}
        sql = text(
            """
            SELECT c.id AS id, 1 - (c.embedding <=> CAST(:qv AS vector)) AS score
            FROM candidates c
            WHERE c.id = ANY(:ids)
              AND c.embedding IS NOT NULL
              AND c.embedding_model = :model
            ORDER BY c.embedding <=> CAST(:qv AS vector), c.id
            LIMIT :k
            """
        ).bindparams(
            bindparam("qv"),
            bindparam("ids", type_=ARRAY(TEXT())),
            bindparam("model"),
            bindparam("k"),
        )
        rows = self.db.execute(
            sql,
            {
                "qv": _vector_literal(query_vector),
                "ids": eligible_ids,
                "model": embedder.name,
                "k": pool_size,
            },
        ).all()
        ranks = {row.id: rank for rank, row in enumerate(rows, start=1)}
        scores = {row.id: float(row.score) for row in rows}
        return ranks, scores


def _hydrate(db: Session, org_id: str, ids: list[str] | None = None) -> list[CandidateDoc]:
    query = (
        select(Candidate)
        .where(Candidate.org_id == org_id)
        .options(selectinload(Candidate.experiences), selectinload(Candidate.educations))
        .order_by(Candidate.id)
    )
    if ids is not None:
        if not ids:
            return []
        query = query.where(Candidate.id.in_(ids))
    rows = db.scalars(query).all()

    # Scoped to the rows being hydrated: this backend only ever materialises
    # the shortlist, and loading every preference in the org to decorate
    # twenty people would give back what that design buys.
    pref_query = select(CandidatePreference).where(CandidatePreference.org_id == org_id)
    if ids is not None:
        pref_query = pref_query.where(CandidatePreference.candidate_id.in_(ids))
    preferences = {row.candidate_id: row for row in db.scalars(pref_query)}

    return [candidate_to_doc(row, preferences.get(row.id)) for row in rows]
