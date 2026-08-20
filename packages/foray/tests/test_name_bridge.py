"""Name-bridge corroboration: a bare name match is not identity."""

from __future__ import annotations

import asyncpg

from foray.config import settings
from foray.db import _asyncpg_dsn
from foray.enrich.contacts import is_placeholder
from foray.enrich.emails import is_usable_commit_email
from foray.enrich.name_bridge import (
    collapse_hits_by_login,
    companies_match,
    corroborate,
    ensure_github_candidate,
    locations_match,
    name_search_query,
    profile_has_slug,
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


def test_name_query_requires_a_full_name():
    assert name_search_query("Ada Lovelace") == 'author-name:"Ada Lovelace"'
    assert name_search_query("Ada") is None
    assert name_search_query("") is None
    assert name_search_query("  ") is None
    assert name_search_query('Ada "quoted" Lovelace') == 'author-name:"Ada quoted Lovelace"'


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


def test_slug_on_the_github_profile_corroborates():
    reasons = corroborate(
        login="ada",
        bucket={"commits": 1},
        user={"bio": "I work at X. https://linkedin.com/in/ada-lovelace", "blog": "", "company": None, "location": None},
        linkedin={"linkedin_slug": "ada-lovelace", "employer": None, "location_metro": None, "location_raw": None},
        sole_login=False,
        min_commits=3,
    )
    assert "linkedin_slug" in reasons


def test_employer_and_location_corroborate():
    reasons = corroborate(
        login="ada",
        bucket={"commits": 1},
        user={"bio": "", "blog": "", "company": "@stripe", "location": "San Francisco, CA"},
        linkedin={
            "linkedin_slug": "someone-else",
            "employer": "Stripe Inc",
            "location_metro": "San Francisco",
            "location_raw": "SF",
        },
        sole_login=False,
        min_commits=3,
    )
    assert "employer" in reasons
    assert "location" in reasons


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


def test_locations_match_substring():
    assert locations_match("San Francisco, CA", "San Francisco", None) is True
    assert locations_match("NYC", None, "New York") is False  # 'nyc' vs 'new york'
    assert locations_match("", "San Francisco", None) is False


def test_profile_has_slug_reads_bio_and_blog():
    assert profile_has_slug({"bio": "hi", "blog": "https://linkedin.com/in/ada-lovelace"}, "ada-lovelace")
    assert not profile_has_slug({"bio": "nope", "blog": ""}, "ada-lovelace")


def test_incident_addresses_would_still_be_written_on_the_github_row():
    for email in ("mail@danijar.com", "email@jamie.sh", "mail@taeuk.me"):
        assert is_usable_commit_email(email)
        assert not is_placeholder(email)


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
