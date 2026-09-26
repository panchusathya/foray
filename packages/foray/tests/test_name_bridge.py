"""Name-bridge corroboration: a bare name match is not identity."""

from __future__ import annotations

import asyncpg

from foray.config import settings
from foray.db import _asyncpg_dsn
from foray.enrich.name_bridge import (
    collapse_hits_by_login,
    companies_match,
    corroborate,
    ensure_github_candidate,
)
from foray.ingest.resolve import queue_pair
from tests.dbutil import requires_db

# Captured-shape Search Commits items for author-name:"David Lee" — several
# GitHub logins, none of which should auto-pass without extra evidence.
DAVID_LEE_ITEMS = [
    {
        "commit": {"author": {"email": "dlee@gmail.com", "date": "2024-01-01T00:00:00Z"}},
        "author": {"login": "dlee1", "id": 11},
    },
    {
        "commit": {"author": {"email": "david@other.com", "date": "2024-02-01T00:00:00Z"}},
        "author": {"login": "dlee2", "id": 12},
    },
    {
        "commit": {"author": {"email": "lee@stanford.edu", "date": "2023-01-01T00:00:00Z"}},
        "author": {"login": "dlee1", "id": 11},
    },
    {
        "commit": {"author": {"email": "ghost@gmail.com", "date": "2024-03-01T00:00:00Z"}},
        "author": None,
    },
]


def test_collapse_drops_unattributed_and_groups_by_login():
    by_login = collapse_hits_by_login(DAVID_LEE_ITEMS)
    assert set(by_login) == {"dlee1", "dlee2"}
    assert by_login["dlee1"]["commits"] == 2
    assert "dlee@gmail.com" in by_login["dlee1"]["emails"]
    assert "ghost@gmail.com" not in by_login["dlee1"]["emails"]
    assert "ghost@gmail.com" not in by_login["dlee2"]["emails"]


def test_uncorroborated_common_name_is_rejected():
    by_login = collapse_hits_by_login(DAVID_LEE_ITEMS)
    linkedin = {
        "linkedin_slug": "david-lee-99abc",
        "employer": "Stripe",
        "location_metro": "San Francisco",
        "location_raw": "San Francisco Bay Area",
    }
    # Bare GitHub users, no overlapping company/location/slug.
    for login, bucket in by_login.items():
        reasons = corroborate(
            login=login,
            bucket=bucket,
            user={"login": login, "bio": "", "blog": "", "company": None, "location": None},
            linkedin=linkedin,
            sole_login=False,
            min_commits=3,
        )
        assert reasons == [], login


def test_rare_name_requires_a_single_login_and_enough_commits():
    reasons = corroborate(
        login="ada",
        bucket={"commits": 8},
        user={"bio": "", "blog": "", "company": None, "location": None},
        linkedin={"linkedin_slug": None, "employer": None, "location_metro": None, "location_raw": None},
        sole_login=True,
        min_commits=3,
    )
    assert "rare_name" in reasons
    too_few = corroborate(
        login="ada",
        bucket={"commits": 1},
        user={"bio": "", "blog": "", "company": None, "location": None},
        linkedin={"linkedin_slug": None, "employer": None, "location_metro": None, "location_raw": None},
        sole_login=True,
        min_commits=3,
    )
    assert too_few == []
    not_sole = corroborate(
        login="ada",
        bucket={"commits": 8},
        user={"bio": "", "blog": "", "company": None, "location": None},
        linkedin={"linkedin_slug": None, "employer": None, "location_metro": None, "location_raw": None},
        sole_login=False,
        min_commits=3,
    )
    assert not_sole == []


def test_companies_match_strips_suffixes_and_at():
    assert companies_match("@Stripe", "Stripe Inc") is True
    assert companies_match("Google LLC", "Alphabet") is False
    assert companies_match("", "Stripe") is False
    assert companies_match("ab", "ab") is False  # too short


@requires_db
async def test_ensure_github_candidate_mints_a_row_then_is_idempotent():
    """The LinkedIn→GitHub hole: a corroborated login that harvest never saw
    still has to become a candidates row before a merge can be queued."""
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    tr = conn.transaction()
    await tr.start()
    try:
        user = {
            "login": "nb_mint_ada",
            "id": None,
            "name": "Ada Lovelace",
            "location": "London",
            "company": "@analytical",
        }
        cid, created = await ensure_github_candidate(conn, user)
        assert created is True
        cid2, created2 = await ensure_github_candidate(conn, user)
        assert created2 is False and cid2 == cid
        li_id = await conn.fetchval(
            "insert into candidates (login, source, name, linkedin_slug) "
            "values ('li:ada-lovelace-test', 'linkedin', 'Ada Lovelace', 'ada-lovelace-test') "
            "returning id"
        )
        await conn.execute(
            "insert into candidate_signals (candidate_id, title) values ($1, 'Software Engineer')",
            li_id,
        )
        await queue_pair(
            conn,
            primary_id=cid,
            merged_id=li_id,
            tier="commit_name",
            evidence={"login": "nb_mint_ada", "reasons": ["employer"]},
        )
        status = await conn.fetchval(
            "select status from candidate_merges where primary_id=$1 and merged_id=$2",
            cid,
            li_id,
        )
        assert status == "pending"
    finally:
        await tr.rollback()
        await conn.close()
