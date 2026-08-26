"""Entity resolution: link LinkedIn-only rows to existing GitHub identities.

    python -m foray.ingest.resolve --tier linkedin_url [--apply]
    python -m foray.ingest.resolve --tier business_email [--apply]
    python -m foray.ingest.resolve --pending          # review queue
    python -m foray.ingest.resolve --revert MERGE_ID

Dry-run by default. Two tiers may auto-apply — an exact profile-slug match and
an exact non-guess work-email match — and both additionally require 1:1
exclusivity: if either side matches more than one row, the pair is queued for
review instead. Name-based tiers only ever queue. Nothing is deleted: the
loser gets ``merged_into`` and every read path filters it out, so a bad merge
reverts cleanly.

"Reverts cleanly" is a property of two things that have to hold together:

  * ``apply_merge`` records what it changed in ``candidate_merges.restore``
    and ``revert_merge`` replays exactly that, instead of reconstructing a
    guess at the pre-merge state. The guess used to leave the winner carrying
    the loser's identity and the loser's *purchased* addresses — a personal
    address bought for one person left on a different one, which is where
    outreach reads ``primary_email`` from;
  * a decided pair (rejected or reverted) is invisible to ``find_pairs``. A
    human's "these are not the same person" is a fact about the pair, not
    about one run, and without it the next ``--apply`` re-found the pair and
    re-made the merge that was just rejected.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from ..db import close_pool, get_pool
from ..log import get_logger

log = get_logger("ingest.resolve")

# Local-parts that are shared inboxes, not a person — never merge on them.
SHARED_LOCAL_PARTS = {
    "info", "dev", "team", "hello", "admin", "contact", "support", "jobs",
    "hr", "sales", "office", "mail",
}

# A pair a human has already decided against. Not scoped to the tier: the
# rejection is about the two people, so a pair rejected on the slug tier must
# not come back through the business-email one. Without this clause `--revert
# 42` was undone by the next `--tier … --apply`, which re-found the same 1:1
# exclusive pair and re-applied the merge the human had just thrown out.
_NOT_DECIDED_AGAINST = """
          and not exists (
              select 1 from candidate_merges m
              where m.primary_id = g.id and m.merged_id = l.id
                and m.status in ('rejected', 'reverted')
          )
"""

_TIER_SQL = {
    # LinkedIn row's slug == a GitHub row's known linkedin_slug (or its
    # linkedin_url containing the slug), both unmerged.
    "linkedin_url": f"""
        select g.id as primary_id, l.id as merged_id,
               l.linkedin_slug as evidence_value
        from candidates l
        join candidates g
          on g.source in ('github', 'both')
         and g.id <> l.id
         and (g.linkedin_slug = l.linkedin_slug
              or (g.linkedin_url is not null
                  and lower(g.linkedin_url) like '%/in/' || l.linkedin_slug || '%'))
        where l.source = 'linkedin'
          and l.merged_into is null
          and g.merged_into is null
          and l.linkedin_slug is not null
          {_NOT_DECIDED_AGAINST}
    """,
    "business_email": f"""
        select g.id as primary_id, l.id as merged_id, le.email as evidence_value
        from candidates l
        join candidate_emails le on le.candidate_id = l.id
             and le.source = 'linkedin' and le.is_work
        join candidate_emails ge on lower(ge.email) = lower(le.email)
             and ge.candidate_id <> l.id and ge.source <> 'guess'
        join candidates g on g.id = ge.candidate_id
             and g.source in ('github', 'both') and g.merged_into is null
        where l.source = 'linkedin'
          and l.merged_into is null
          and split_part(lower(le.email), '@', 1) <> all($1::text[])
          {_NOT_DECIDED_AGAINST}
    """,
}


async def find_pairs(conn, tier: str) -> list[dict]:
    params = ([sorted(SHARED_LOCAL_PARTS)],) if tier == "business_email" else ()
    rows = await conn.fetch(_TIER_SQL[tier], *params)
    pairs = [dict(r) for r in rows]

    # 1:1 exclusivity — the guard against collapsing two different people.
    # If either side appears in more than one pair, every pair touching it is
    # demoted to the review queue.
    from collections import Counter

    primary_counts = Counter(p["primary_id"] for p in pairs)
    merged_counts = Counter(p["merged_id"] for p in pairs)
    for pair in pairs:
        pair["exclusive"] = (
            primary_counts[pair["primary_id"]] == 1 and merged_counts[pair["merged_id"]] == 1
        )
    return pairs


async def apply_merge(conn, *, primary_id: int, merged_id: int, tier: str, evidence: dict) -> None:
    """One transaction; reversible. The winner is re-derived, re-embedded and
    re-scored because its evidence just changed.

    Every mutation records what it displaced into the ``restore`` blob, which
    is the only thing ``revert_merge`` reads. Nothing here is reconstructible
    afterwards: which rows moved and which were already the winner's is not a
    question the post-merge database can answer.
    """
    async with conn.transaction():
        before = await conn.fetchrow(
            "select source, linkedin_url from candidates where id = $1", primary_id
        )
        moved_profiles = await conn.fetch(
            """
            update raw_profiles set candidate_id = $1
            where candidate_id = $2 and source = 'linkedin'
              and not exists (select 1 from raw_profiles
                              where candidate_id = $1 and source = 'linkedin')
            returning candidate_id
            """,
            primary_id,
            merged_id,
        )
        # Emails follow, EXCEPT the ones the winner already holds -- those
        # cannot move (the (candidate_id, email) unique key) and are left on
        # the loser rather than deleted. Deleting them made revert lossy for no
        # gain: the loser is merged_into and every read path filters it out, so
        # a duplicate row sitting there costs nothing, while a deleted one is
        # gone whatever the human decides later. A soft-delete flag would have
        # worked too and was rejected: it puts a new "and not deleted" that
        # every reader of candidate_emails must remember on the path to a send
        # address, which is a worse failure than a redundant row.
        moved_emails = await conn.fetch(
            """
            update candidate_emails e set candidate_id = $1
            where e.candidate_id = $2
              and not exists (select 1 from candidate_emails w
                              where w.candidate_id = $1 and lower(w.email) = lower(e.email))
            returning e.id
            """,
            primary_id,
            merged_id,
        )
        # linkedin_url only, never linkedin_slug: the slug carries a unique
        # index and stays the LinkedIn row's identity (its login is
        # 'li:'||slug, and the importer rewrites it on every re-import), so
        # copying it here is a constraint violation waiting for the first
        # winner that has no slug of its own -- which is every pair this tier
        # finds, because the join can only fire through linkedin_url.
        await conn.execute(
            """
            update candidates
               set source = 'both',
                   linkedin_url = coalesce(linkedin_url,
                       (select linkedin_url from candidates where id = $2)),
                   embed_blob = null, embedding = null, embedded_at = null
             where id = $1
            """,
            primary_id,
            merged_id,
        )
        await conn.execute("delete from archetype_scores where candidate_id = $1", primary_id)
        await conn.execute(
            "update candidates set merged_into = $1 where id = $2", primary_id, merged_id
        )
        restore = {
            "primary_source": before["source"],
            "primary_linkedin_url": before["linkedin_url"],
            "moved_raw_profile": bool(moved_profiles),
            "moved_email_ids": [int(r["id"]) for r in moved_emails],
        }
        await conn.execute(
            """
            insert into candidate_merges
                (primary_id, merged_id, tier, status, evidence, restore, decided_at)
            values ($1, $2, $3, 'applied', $4::jsonb, $5::jsonb, now())
            """,
            primary_id,
            merged_id,
            tier,
            json.dumps(evidence),
            json.dumps(restore),
        )


async def queue_pair(conn, *, primary_id: int, merged_id: int, tier: str, evidence: dict) -> None:
    """Add a pair to the review queue unless it has already been decided.

    "Already decided" is deliberately tier-blind for the terminal statuses:
    a pair a human rejected, reverted or applied must not reappear in the
    queue because a different tier found the same two rows. Only the pending /
    same-tier duplicate check is tier-scoped, as before. enrich.name_bridge
    queues through here too, which is the other way a rejected pair used to
    come back.
    """
    exists = await conn.fetchval(
        "select 1 from candidate_merges where primary_id=$1 and merged_id=$2 "
        "and (tier=$3 or status in ('applied', 'rejected', 'reverted'))",
        primary_id,
        merged_id,
        tier,
    )
    if not exists:
        await conn.execute(
            """
            insert into candidate_merges (primary_id, merged_id, tier, status, evidence)
            values ($1, $2, $3, 'pending', $4::jsonb)
            """,
            primary_id,
            merged_id,
            tier,
            json.dumps(evidence),
        )


async def revert_merge(conn, merge_id: int) -> bool:
    """Undo exactly what ``apply_merge`` recorded, and mark the pair decided.

    Everything here is driven by the ``restore`` blob rather than by a
    predicate over the current state. The predicates it replaces were each
    wrong in a way that only showed up later:

      * ``source = 'github'`` demoted a winner that had been 'both' before the
        merge, hiding its own LinkedIn evidence from every source filter;
      * moving back every ``source = 'linkedin'`` raw_profile / address moved
        the WINNER'S own rows onto the loser when the merge had not moved
        anything, and left behind everything the merge HAD moved under any
        other source -- including ``contactout_personal``, a purchased address
        that then stayed on a stranger as their ``primary_email`` while the
        person it was bought for kept a stamped ``contactout_checked_at`` and
        so was never looked up again.
    """
    row = await conn.fetchrow(
        "select * from candidate_merges where id = $1 and status = 'applied'", merge_id
    )
    if row is None:
        return False
    primary_id, merged_id = row["primary_id"], row["merged_id"]
    restore = row["restore"]
    if isinstance(restore, str):
        restore = json.loads(restore)
    restore = restore or {}
    # Applied before migration 015, so there is no record to replay. Fall back
    # to the old predicates: they are wrong in the ways described above, but a
    # row written under the old code cannot be undone better than it was
    # written, and doing less would strand more.
    legacy = not restore
    async with conn.transaction():
        if legacy or restore.get("moved_raw_profile"):
            await conn.execute(
                "update raw_profiles set candidate_id = $2 "
                "where candidate_id = $1 and source = 'linkedin'",
                primary_id,
                merged_id,
            )
        email_ids = [int(i) for i in restore.get("moved_email_ids") or []]
        if email_ids:
            await conn.execute(
                "update candidate_emails set candidate_id = $1 where id = any($2::bigint[])",
                merged_id,
                email_ids,
            )
        elif legacy:
            await conn.execute(
                "update candidate_emails set candidate_id = $2 "
                "where candidate_id = $1 and source = 'linkedin'",
                primary_id,
                merged_id,
            )
        # Both rows carry a blob describing evidence they no longer hold.
        # ``source`` and ``linkedin_url`` go back to the values the winner had
        # before the merge -- a null url included, or the pipeline would read
        # the loser's slug off the winner and re-find the pair. A legacy row
        # never recorded the url, and clearing one the winner may have owned
        # all along is worse than leaving it, so only ``source`` is touched.
        if legacy:
            await conn.execute(
                "update candidates set source = 'github', embed_blob = null, "
                "embedding = null, embedded_at = null where id = $1",
                primary_id,
            )
        else:
            await conn.execute(
                """
                update candidates
                   set source = $2,
                       linkedin_url = $3,
                       embed_blob = null, embedding = null, embedded_at = null
                 where id = $1
                """,
                primary_id,
                restore.get("primary_source") or "github",
                restore.get("primary_linkedin_url"),
            )
        await conn.execute("delete from archetype_scores where candidate_id = any($1::bigint[])",
                           [primary_id, merged_id])
        await conn.execute(
            "update candidates set merged_into = null where id = $1", merged_id
        )
        await conn.execute(
            "update candidate_merges set status = 'reverted', decided_at = now() where id = $1",
            merge_id,
        )
    return True


async def amain(args: argparse.Namespace) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if args.revert:
            ok = await revert_merge(conn, args.revert)
            print("reverted" if ok else "no applied merge with that id")
            await close_pool()
            return 0 if ok else 1

        if args.pending:
            rows = await conn.fetch(
                """
                select m.id, m.tier, m.evidence,
                       g.login as primary_login, l.login as merged_login,
                       g.name as primary_name, l.name as merged_name
                from candidate_merges m
                join candidates g on g.id = m.primary_id
                join candidates l on l.id = m.merged_id
                where m.status = 'pending'
                order by m.id
                """
            )
            for row in rows:
                print(
                    f"#{row['id']:>5} [{row['tier']}] {row['primary_login']} "
                    f"({row['primary_name']}) <= {row['merged_login']} ({row['merged_name']})"
                )
            print(f"{len(rows)} pending")
            await close_pool()
            return 0

        if args.scan:
            # Nightly dry-run: surface pair counts for the auto-apply tiers
            # without applying anything. Name-bridge rows arrive via
            # enrich.name_bridge into the pending queue, not this scan.
            total_auto = total_review = 0
            for tier in _TIER_SQL:
                pairs = await find_pairs(conn, tier)
                auto = sum(1 for p in pairs if p["exclusive"])
                review = len(pairs) - auto
                total_auto += auto
                total_review += review
                print(f"tier {tier}: {auto} auto-mergeable, {review} need review")
            pending = await conn.fetchval(
                "select count(*) from candidate_merges where status = 'pending'"
            )
            print(
                f"scan: {total_auto} auto-mergeable across tiers, "
                f"{total_review} need review, {pending} already pending"
            )
            await close_pool()
            return 0

        pairs = await find_pairs(conn, args.tier)
        auto = [p for p in pairs if p["exclusive"]]
        review = [p for p in pairs if not p["exclusive"]]
        print(f"tier {args.tier}: {len(auto)} auto-mergeable, {len(review)} need review")

        if args.apply:
            for pair in auto:
                await apply_merge(
                    conn,
                    primary_id=pair["primary_id"],
                    merged_id=pair["merged_id"],
                    tier=args.tier,
                    evidence={"value": str(pair["evidence_value"])},
                )
            for pair in review:
                await queue_pair(
                    conn,
                    primary_id=pair["primary_id"],
                    merged_id=pair["merged_id"],
                    tier=args.tier,
                    evidence={"value": str(pair["evidence_value"]), "reason": "not 1:1"},
                )
            print(f"applied {len(auto)}, queued {len(review)}")
        else:
            for pair in auto[:20]:
                print(f"  would merge candidate {pair['merged_id']} -> {pair['primary_id']} "
                      f"({pair['evidence_value']})")
            print("dry-run: pass --apply to execute")
    await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Link LinkedIn rows to GitHub identities.")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--tier", choices=tuple(_TIER_SQL))
    group.add_argument("--pending", action="store_true", help="list the review queue")
    group.add_argument(
        "--scan",
        action="store_true",
        help="dry-run every auto-apply tier (nightly; never applies)",
    )
    group.add_argument("--revert", type=int, metavar="MERGE_ID")
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
