"""Merge/revert symmetry, and the pipeline's memory of a human's decision.

Two production shapes are pinned here:

1. a reverted merge left the winner carrying the loser's LinkedIn identity and
   the loser's *purchased* addresses -- a ContactOut personal address bought
   for one person, sitting on a different one as their primary_email, while
   the person it was bought for kept a stamped contactout_checked_at and so
   was never looked up again;
2. reverting did not stop the pipeline: find_pairs reads only `candidates`, so
   the next `--tier ... --apply` re-found the same 1:1 exclusive pair and
   re-made the merge the human had just thrown out.

No database: these drive the two functions against a recording connection, so
what is asserted is the statements they issue and the restore record they keep.
"""

from __future__ import annotations

import json

import pytest

from foray.ingest.resolve import _TIER_SQL, apply_merge, queue_pair, revert_merge


class FakeConn:
    """Records every statement; replays queued results for fetch/fetchrow."""

    def __init__(self, *, fetchrow=(), fetch=(), fetchval=()) -> None:
        self.executed: list[tuple] = []
        self._fetchrow = list(fetchrow)
        self._fetch = list(fetch)
        self._fetchval = list(fetchval)

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                conn.executed.append(("BEGIN",))
                return self

            async def __aexit__(self, *_exc):
                conn.executed.append(("COMMIT",))

        return _Tx()

    async def execute(self, *args):
        self.executed.append(args)

    async def fetch(self, *args):
        self.executed.append(args)
        return self._fetch.pop(0) if self._fetch else []

    async def fetchrow(self, *args):
        self.executed.append(args)
        return self._fetchrow.pop(0) if self._fetchrow else None

    async def fetchval(self, *args):
        self.executed.append(args)
        return self._fetchval.pop(0) if self._fetchval else None

    def statements(self, needle: str) -> list[tuple]:
        return [a for a in self.executed if a and isinstance(a[0], str) and needle in a[0]]

    def one(self, needle: str) -> tuple:
        [stmt] = self.statements(needle)
        return stmt


async def _apply(primary_before: dict, *, moved_email_ids=(7,), moved_profile=True) -> FakeConn:
    conn = FakeConn(
        fetchrow=[primary_before],
        fetch=[
            [{"candidate_id": 1}] if moved_profile else [],
            [{"id": i} for i in moved_email_ids],
        ],
    )
    await apply_merge(
        conn, primary_id=1, merged_id=2, tier="linkedin_url", evidence={"value": "jane-doe"}
    )
    return conn


def _restore_of(conn: FakeConn) -> dict:
    insert = conn.one("insert into candidate_merges")
    return json.loads(insert[5])


class TestApplyRecordsWhatItChanged:
    async def test_the_winner_never_takes_the_slug(self):
        """linkedin_slug carries a unique index and the LinkedIn row keeps it
        (its login is 'li:'||slug, rewritten on every re-import). Copying it
        onto the winner is a constraint violation for exactly the pairs this
        tier finds -- the join can only fire through linkedin_url."""
        conn = await _apply({"source": "github", "linkedin_url": None})
        update = conn.one("set source = 'both'")
        assert "linkedin_slug" not in update[0]
        assert "linkedin_url" in update[0]

    async def test_the_restore_blob_captures_the_pre_merge_identity(self):
        conn = await _apply({"source": "both", "linkedin_url": "https://li/in/own"})
        assert _restore_of(conn) == {
            "primary_source": "both",
            "primary_linkedin_url": "https://li/in/own",
            "moved_raw_profile": True,
            "moved_email_ids": [7],
        }

    async def test_duplicate_addresses_are_left_on_the_loser_not_deleted(self):
        """The loser is merged_into and every read path filters it out, so a
        redundant row costs nothing -- while a deleted one is gone whatever the
        human decides later."""
        conn = await _apply({"source": "github", "linkedin_url": None})
        assert conn.statements("delete from candidate_emails") == []
        [move] = conn.statements("update candidate_emails")
        assert "not exists" in move[0] and "returning" in move[0]


class TestRevertReplaysTheRecord:
    async def test_identity_and_purchased_addresses_go_back(self):
        row = {
            "primary_id": 1,
            "merged_id": 2,
            "restore": json.dumps(
                {
                    "primary_source": "github",
                    "primary_linkedin_url": None,
                    "moved_raw_profile": True,
                    "moved_email_ids": [7, 9],
                }
            ),
        }
        conn = FakeConn(fetchrow=[row])
        assert await revert_merge(conn, 42) is True

        emails = conn.one("update candidate_emails set candidate_id = $1 where id = any")
        assert emails[1] == 2 and emails[2] == [7, 9]
        candidates = conn.one("set source = $2")
        assert candidates[2] == "github"
        assert candidates[3] is None  # the url the merge wrote is cleared

    async def test_a_winner_that_was_already_both_is_not_demoted(self):
        row = {
            "primary_id": 1,
            "merged_id": 2,
            "restore": {
                "primary_source": "both",
                "primary_linkedin_url": "https://li/in/own",
                "moved_raw_profile": False,
                "moved_email_ids": [],
            },
        }
        conn = FakeConn(fetchrow=[row])
        await revert_merge(conn, 42)
        candidates = conn.one("set source = $2")
        assert candidates[2] == "both"
        assert candidates[3] == "https://li/in/own"
        # apply_merge moved no raw_profile, so revert must not take the
        # winner's own one away.
        assert conn.statements("update raw_profiles") == []

    async def test_a_merge_applied_before_the_restore_column_still_reverts(self):
        """Rows written by the old code have nothing to replay. Falling back to
        the old predicates is wrong in the documented ways, but doing less
        would strand more."""
        conn = FakeConn(fetchrow=[{"primary_id": 1, "merged_id": 2, "restore": {}}])
        await revert_merge(conn, 42)
        assert conn.statements("update raw_profiles")
        assert conn.statements("update candidate_emails set candidate_id = $2")
        assert conn.statements("set source = 'github'")

    async def test_a_merge_that_is_not_applied_is_a_no_op(self):
        conn = FakeConn(fetchrow=[None])
        assert await revert_merge(conn, 42) is False
        assert conn.statements("update candidates") == []


class TestTheDecisionSticks:
    @pytest.mark.parametrize("tier", sorted(_TIER_SQL))
    def test_every_auto_apply_tier_skips_a_decided_pair(self, tier):
        sql = _TIER_SQL[tier]
        assert "candidate_merges" in sql
        assert "'rejected', 'reverted'" in sql

    async def test_queue_pair_refuses_a_pair_decided_at_another_tier(self):
        conn = FakeConn(fetchval=[1])
        await queue_pair(
            conn, primary_id=1, merged_id=2, tier="commit_name", evidence={"value": "x"}
        )
        assert conn.statements("insert into candidate_merges") == []
        check = conn.one("select 1 from candidate_merges")
        assert "'applied', 'rejected', 'reverted'" in check[0]

    async def test_an_undecided_pair_is_still_queued(self):
        conn = FakeConn(fetchval=[None])
        await queue_pair(
            conn, primary_id=1, merged_id=2, tier="commit_name", evidence={"value": "x"}
        )
        assert conn.statements("insert into candidate_merges")
