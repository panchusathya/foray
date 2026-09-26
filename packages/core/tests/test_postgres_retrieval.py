"""Postgres retrieval: filters, incremental upsert, overlap with the RAM index.

Skipped unless ``JUICEBOX_TEST_DATABASE_URL`` points at PostgreSQL — SQLite has
no tsvector or pgvector, and that is the point of the in-process backend.
"""

from __future__ import annotations

import os

import pytest

from juicebox.repository import upsert_candidates
from juicebox.search.engine import get_engine
from juicebox.search.index import get_memory_index
from juicebox.search.parser import heuristic_parse

pytestmark = pytest.mark.skipif(
    not os.environ.get("JUICEBOX_TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="Postgres retrieval tests need JUICEBOX_TEST_DATABASE_URL",
)


def _load(session, account, sample_candidates):
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
    session.commit()


class TestPostgresBackend:

    def test_hard_filter_counts_match_the_in_process_index(
        self, session, account, sample_candidates
    ):
        _load(session, account, sample_candidates)
        outcome = get_engine().search(
            session,
            org_id=account["org_id"],
            query="backend engineer",
            overrides={"excluded_companies": ["ramp"]},
            persist=False,
        )
        assert all(r.doc.full_name != "Cleo Rahman" for r in outcome.results)
        assert outcome.excluded.get("excluded_company") == 1

    def test_new_row_is_searchable_without_rebuilding_a_corpus(
        self, session, account, sample_candidates
    ):
        _load(session, account, sample_candidates)
        upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[
                {
                    "source": "test",
                    "source_id": "new-1",
                    "full_name": "Dee Wu",
                    "skills": ["Go", "PostgreSQL"],
                    "experiences": [
                        {
                            "company": "Stripe",
                            "title": "Staff Backend Engineer",
                            "start_date": "2018-01",
                            "is_current": True,
                        }
                    ],
                }
            ],
        )
        session.commit()
        names = {
            r.doc.full_name
            for r in get_engine().search(
                session,
                org_id=account["org_id"],
                query="staff backend engineer with go",
                persist=False,
            ).results
        }
        assert "Dee Wu" in names

    def test_top_hits_overlap_the_memory_index(self, session, account, sample_candidates):
        """Same people in the top 3; scores may differ (ts_rank is not BM25)."""
        _load(session, account, sample_candidates)
        query = "senior machine learning engineer in San Francisco with pytorch"

        pg = get_engine().search(
            session, org_id=account["org_id"], query=query, limit=3, persist=False
        )
        mem_pool = get_memory_index(session, account["org_id"]).search_pool(
            heuristic_parse(query), pool_size=3
        )
        pg_names = [r.doc.full_name for r in pg.results[:3]]
        mem_names = [doc.full_name for doc in mem_pool.docs[:3]]
        assert pg_names[0] == mem_names[0]
        assert set(pg_names) == set(mem_names)
