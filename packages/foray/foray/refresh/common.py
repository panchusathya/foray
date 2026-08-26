"""Shared refresh plumbing: refresh_runs logging so a silent failure three weeks
later is debuggable, and the one definition of month-to-date LLM spend that
every unattended scoring path is measured against.
"""

from __future__ import annotations

from datetime import datetime

from ..config import settings


class RefreshRun:
    """Context manager that logs one row into refresh_runs (start/end/counts)."""

    def __init__(self, conn, job: str) -> None:
        self.conn = conn
        self.job = job
        self.id: int | None = None
        self.rows = 0
        self.api_calls = 0
        self.errors = 0
        self.notes: str | None = None

    async def __aenter__(self) -> RefreshRun:
        self.id = await self.conn.fetchval(
            "insert into refresh_runs (job) values ($1) returning id", self.job
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self.errors += 1
            self.notes = (self.notes or "") + f" error: {exc}"[:400]
        await self.conn.execute(
            "update refresh_runs set ended_at = now(), rows_touched = $1, "
            "api_calls = $2, errors = $3, notes = $4 where id = $5",
            self.rows,
            self.api_calls,
            self.errors,
            self.notes,
            self.id,
        )


# Every unattended job that can spend on scoring, so one cap covers all of
# them. A job missing from here is spend the cap cannot see.
SCORING_JOBS = ("nightly.score", "slow.score")


async def scoring_calls_since(conn, since: datetime) -> int:
    """How many scoring calls were actually PAID FOR since ``since``.

    Not a row-count delta on archetype_scores, which is what the nightly used
    to record: score.run upserts, so a rescore -- the expensive case, every
    pair in the pool after one edit to a rubric -- moves that count by zero and
    reported ``rows_touched = 0`` for a full 10,000-call night. The spend cap
    read those zeroes and concluded the month had cost nothing.

    A call is billed whether or not it produced a usable answer, so failures
    (rows in scoring_errors) count exactly like successes. The offline
    heuristic scorer is excluded because it is free by construction.
    """
    scored = await conn.fetchval(
        "select count(*) from archetype_scores "
        "where scored_at >= $1 and model <> 'offline-heuristic'",
        since,
    )
    failed = await conn.fetchval(
        "select count(*) from scoring_errors where created_at >= $1", since
    )
    return int(scored or 0) + int(failed or 0)


async def month_spend(conn) -> float:
    """Month-to-date scoring spend, in dollars."""
    calls = await conn.fetchval(
        "select coalesce(sum(rows_touched), 0) from refresh_runs "
        "where job = any($1::text[]) and started_at > date_trunc('month', now())",
        list(SCORING_JOBS),
    )
    return float(calls or 0) * settings.scoring_usd_per_call


async def calls_left_this_month(conn) -> int:
    """Calls still inside ``monthly_spend_cap_usd``. Never negative."""
    remaining = settings.monthly_spend_cap_usd - await month_spend(conn)
    per_call = settings.scoring_usd_per_call
    if remaining <= 0:
        return 0
    if per_call <= 0:
        # Cost accounting deliberately switched off. A dollar cap cannot bound
        # anything then, so fall back to one night's call budget rather than
        # reporting "no budget left" and stopping scoring altogether.
        return settings.nightly_score_calls
    return int(remaining / per_call)


async def active_logins(conn) -> list[str]:
    rows = await conn.fetch("select login from active_candidates order by login")
    return [r["login"] for r in rows]
