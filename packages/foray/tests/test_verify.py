"""Free corroboration scoring. Pure ``reach_score`` plus Gravatar status mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from foray.enrich.contacts import is_placeholder
from foray.enrich.verify import ReachInputs, gravatar_hash, interpret_gravatar_status, reach_score

NOW = datetime(2026, 8, 20, tzinfo=UTC)


def test_a_thin_single_source_gmail_is_low_but_nonzero():
    score = reach_score(
        ReachInputs(
            source_count=1,
            occurrences=1,
            last_evidence_at=NOW - timedelta(days=2000),
            gravatar_state="unknown",
            mx_present=None,
            is_freemail=True,
            now=NOW,
        )
    )
    assert 20 <= score <= 40


def test_independent_sources_and_recency_and_gravatar_hit_raise_it():
    score = reach_score(
        ReachInputs(
            source_count=2,
            occurrences=8,
            last_evidence_at=NOW - timedelta(days=10),
            gravatar_state="hit",
            mx_present=True,
            is_freemail=True,
            now=NOW,
        )
    )
    assert score >= 70


def test_own_domain_is_worth_more_than_freemail():
    shared = dict(
        source_count=1,
        occurrences=4,
        last_evidence_at=NOW - timedelta(days=30),
        gravatar_state="unknown",
        now=NOW,
    )
    own = reach_score(ReachInputs(**shared, is_freemail=False))
    free = reach_score(ReachInputs(**shared, is_freemail=True))
    assert own == free + 10


def test_gravatar_miss_does_not_degrade_vs_unknown():
    """A miss is not evidence the mailbox is dead. Same score as unknown."""
    base = dict(
        source_count=1,
        occurrences=3,
        last_evidence_at=NOW - timedelta(days=40),
        mx_present=None,
        is_freemail=True,
        now=NOW,
    )
    miss = reach_score(ReachInputs(**base, gravatar_state="miss"))
    unknown = reach_score(ReachInputs(**base, gravatar_state="unknown"))
    hit = reach_score(ReachInputs(**base, gravatar_state="hit"))
    assert miss == unknown
    assert hit == miss + 15


def test_score_is_clamped():
    assert (
        reach_score(
            ReachInputs(
                source_count=9,
                occurrences=100,
                gravatar_state="hit",
                mx_present=True,
                is_freemail=False,
                last_evidence_at=NOW,
                now=NOW,
            )
        )
        <= 100
    )
    assert reach_score(ReachInputs(source_count=0, occurrences=0, gravatar_state="miss")) >= 0


def test_gravatar_status_mapping():
    assert interpret_gravatar_status(200) == "hit"
    assert interpret_gravatar_status(404) == "miss"
    assert interpret_gravatar_status(429) == "unknown"
    assert interpret_gravatar_status(500) == "unknown"
    assert interpret_gravatar_status(0) == "unknown"


def test_gravatar_hash_is_md5_of_lowercased_address():
    assert gravatar_hash("Ada@Example.COM") == gravatar_hash("ada@example.com")
    assert len(gravatar_hash("ada@example.com")) == 32


def test_incident_addresses_still_score_as_real_own_domains():
    for email in ("mail@danijar.com", "email@jamie.sh", "mail@taeuk.me"):
        assert is_placeholder(email) is False
        score = reach_score(
            ReachInputs(
                source_count=1,
                occurrences=5,
                last_evidence_at=NOW - timedelta(days=20),
                gravatar_state="unknown",
                is_freemail=False,
                now=NOW,
            )
        )
        assert score >= 40
