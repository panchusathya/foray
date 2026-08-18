"""Shared refresh plumbing: refresh_runs logging so a silent failure three weeks
later is debuggable.
"""

from __future__ import annotations

from typing import Any


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

    async def __aenter__(self) -> "RefreshRun":
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


async def active_logins(conn) -> list[str]:
    rows = await conn.fetch("select login from active_candidates order by login")
    return [r["login"] for r in rows]
