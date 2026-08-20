"""close_pool() must never hang the process.

CI runs #79, #80 and #83 each burned between 1h35m and 4h19m on a runner
because scripts/seed_synthetic.py --source linkedin awaited close_pool() from
inside `async with pool.acquire()`. Pool.close() waits for every connection to
be released; the only thing that can release that one is the block exit it is
blocking. These tests pin both halves of the fix: the call sites release first,
and close_pool() is bounded even if a future one does not.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

import foray.db as db

FORAY_ROOT = Path(db.__file__).resolve().parent.parent


class _StuckPool:
    """A pool whose graceful close never completes, like a held connection."""

    def __init__(self) -> None:
        self.terminated = False

    async def close(self) -> None:
        await asyncio.Event().wait()

    def terminate(self) -> None:
        self.terminated = True


async def test_close_pool_terminates_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(db, "CLOSE_TIMEOUT_SECONDS", 0.05)
    stuck = _StuckPool()
    monkeypatch.setattr(db, "_pool", stuck)

    with pytest.raises(SystemExit) as caught:
        await asyncio.wait_for(db.close_pool(), timeout=5)

    assert stuck.terminated, "a pool that will not close gracefully must be terminated"
    assert "still holding a connection" in str(caught.value)
    assert db._pool is None, "the module global must be cleared so a retry re-creates it"


async def test_close_pool_is_idempotent(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    await db.close_pool()  # must not raise


def _closes_inside_acquire(path: Path) -> list[int]:
    """Line numbers where close_pool() is awaited inside `async with ...acquire()`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[int] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        acquires = any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "acquire"
            for item in node.items
        )
        if not acquires:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "close_pool"
            ):
                offenders.append(inner.lineno)
    return offenders


@pytest.mark.parametrize(
    "relative",
    [
        "scripts/seed_synthetic.py",
        "foray/enrich/derive.py",
        "foray/embed/run.py",
        "foray/score/run.py",
        "foray/api/app.py",
        "foray/status.py",
    ],
)
def test_no_close_pool_inside_an_acquire_block(relative: str):
    path = FORAY_ROOT / relative
    assert path.exists(), path
    offenders = _closes_inside_acquire(path)
    assert not offenders, (
        f"{relative} awaits close_pool() at line(s) {offenders} while still holding a "
        "pooled connection -- Pool.close() waits for that connection forever"
    )
