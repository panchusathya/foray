"""Nightly phase order: search-bucket jobs must not overlap."""

from __future__ import annotations

import pytest

from foray.cli import _COMMANDS
from foray.config import settings
from foray.refresh.common import SCORING_JOBS, calls_left_this_month, month_spend
from foray.refresh.nightly import _phases, _with_max_calls
from foray.refresh.slow import score_args


def test_search_api_jobs_run_sequentially_after_harvest():
    names = [p.name for p in _phases()]
    assert names.index("harvest") < names.index("commit_search")
    assert names.index("commit_search") < names.index("name_bridge")
    # Own-repo mining (core REST) stays as the emails phase, before commit search.
    assert names.index("emails") < names.index("commit_search")
    # LinkedIn bulk (not Search API) then resolve dry-run so URL joins surface.
    assert names.index("linkedin") < names.index("resolve")
    assert names.index("linkedin") > names.index("emails")


def test_linkedin_phase_uses_the_existing_bulk_runner():
    linkedin = next(p for p in _phases() if p.name == "linkedin")
    assert linkedin.argv[0] == "foray.enrich.linkedin_bulk"
    resolve = next(p for p in _phases() if p.name == "resolve")
    assert "--scan" in resolve.argv
    assert "--apply" not in resolve.argv


class StubConn:
    """Returns one queued value per fetchval, in order."""

    def __init__(self, *values) -> None:
        self.values = list(values)
        self.queries: list[str] = []

    async def fetchval(self, sql, *_args):
        self.queries.append(sql)
        return self.values.pop(0) if self.values else 0


class TestTheMonthlyCapMeasuresSpendNotRows:
    """The cap was computed from `scored_pairs`, a count(*) over
    archetype_scores. score.run upserts, so a RESCORE -- the whole pool after
    one edit to a shared rubric, and by far the most expensive workload -- moved
    that count by zero. The night burned its full budget, recorded
    rows_touched = 0, and _month_spend answered $0.00 forever.
    """

    def test_the_score_phase_records_calls_made(self):
        score = next(p for p in _phases() if p.name == "score")
        assert score.counts_calls is True
        assert all(p.counts_calls is False for p in _phases() if p.name != "score")

    async def test_failed_calls_count_as_spend(self):
        """A call billed by OpenRouter and rejected by parse_scoring_json cost
        the same as one that worked."""
        from foray.refresh.common import scoring_calls_since

        conn = StubConn(90, 10)
        assert await scoring_calls_since(conn, "2026-08-01") == 100
        assert "archetype_scores" in conn.queries[0]
        assert "scoring_errors" in conn.queries[1]

    async def test_offline_scoring_is_not_charged(self):
        from foray.refresh.common import scoring_calls_since

        conn = StubConn(0, 0)
        await scoring_calls_since(conn, "2026-08-01")
        assert "offline-heuristic" in conn.queries[0]

    async def test_every_unattended_scoring_job_is_summed(self):
        conn = StubConn(1000)
        spend = await month_spend(conn)
        assert spend == pytest.approx(1000 * settings.scoring_usd_per_call)
        assert set(SCORING_JOBS) == {"nightly.score", "slow.score"}

    async def test_a_spent_month_leaves_no_calls(self, monkeypatch):
        monkeypatch.setattr(settings, "monthly_spend_cap_usd", 40.0)
        monkeypatch.setattr(settings, "scoring_usd_per_call", 0.0003)
        assert await calls_left_this_month(StubConn(200_000)) == 0
        assert await calls_left_this_month(StubConn(0)) == 133_333


class TestAnUnattendedRunCannotOvershoot:
    def test_the_night_is_bounded_by_what_is_left_of_the_month(self):
        score = next(p for p in _phases() if p.name == "score")
        bounded = _with_max_calls(score, 12)
        idx = bounded.argv.index("--max-calls")
        assert bounded.argv[idx + 1] == "12"
        assert score.argv[idx + 1] != "12", "the original phase must not be mutated"

    def test_the_quarterly_refresh_pairs_yes_with_a_ceiling(self, monkeypatch):
        """--yes is exactly the flag that suppresses the spend confirmation, so
        the quarterly job may only pass it alongside --max-calls."""
        monkeypatch.setattr(settings, "openrouter_api_key", "sk-test")
        argv = score_args(calls_left=5_000)
        assert argv is not None
        assert "--yes" in argv
        assert argv[argv.index("--max-calls") + 1] == "5000"

    def test_the_quarterly_refresh_stops_at_the_month_cap(self, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_api_key", "sk-test")
        assert score_args(calls_left=0) is None

    def test_no_key_still_means_offline_and_never_yes(self, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_api_key", "")
        argv = score_args(calls_left=0)
        assert argv is not None and "--offline" in argv and "--yes" not in argv


def test_cli_wires_the_new_commands():
    assert _COMMANDS["commit-search"] == "foray.enrich.commit_search"
    assert _COMMANDS["name-bridge"] == "foray.enrich.name_bridge"
    assert _COMMANDS["verify"] == "foray.enrich.verify"
    assert _COMMANDS["resolve"] == "foray.ingest.resolve"
    assert _COMMANDS["linkedin"] == "foray.enrich.linkedin_bulk"
