"""Free corroboration scoring. Pure ``reach_score`` plus Gravatar status mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from foray.enrich.contacts import is_placeholder
from foray.enrich.verify import ReachInputs, interpret_gravatar_status, reach_score

NOW = datetime(2026, 8, 20, tzinfo=UTC)


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


def test_gravatar_status_mapping():
    assert interpret_gravatar_status(200) == "hit"
    assert interpret_gravatar_status(404) == "miss"
    assert interpret_gravatar_status(429) == "unknown"
    assert interpret_gravatar_status(500) == "unknown"
    assert interpret_gravatar_status(0) == "unknown"


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
