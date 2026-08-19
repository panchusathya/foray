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

_TIER_SQL = {
    # LinkedIn row's slug == a GitHub row's known linkedin_slug (or its
    # linkedin_url containing the slug), both unmerged.
    "linkedin_url": """
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
    """,
    "business_email": """
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
    re-scored because its evidence just changed."""
    async with conn.transaction():
        await conn.execute(
            """
            update raw_profiles set candidate_id = $1
            where candidate_id = $2 and source = 'linkedin'
              and not exists (select 1 from raw_profiles
                              where candidate_id = $1 and source = 'linkedin')
            """,
            primary_id,
            merged_id,
        )
        # Emails follow; the (candidate_id, email) unique key means an address
        # both rows knew stays on the winner via the loser's row deletion.
        await conn.execute(
            """
            delete from candidate_emails le
            where le.candidate_id = $2
              and exists (select 1 from candidate_emails
                          where candidate_id = $1 and lower(email) = lower(le.email))
            """,
            primary_id,
            merged_id,
        )
        await conn.execute(
            "update candidate_emails set candidate_id = $1 where candidate_id = $2",
            primary_id,
            merged_id,
        )
        await conn.execute(
            """
            update candidates
               set source = 'both',
                   linkedin_slug = coalesce(linkedin_slug,
                       (select linkedin_slug from candidates where id = $2)),
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
        await conn.execute(
            """
            insert into candidate_merges (primary_id, merged_id, tier, status, evidence, decided_at)
            values ($1, $2, $3, 'applied', $4::jsonb, now())
            """,
            primary_id,
            merged_id,
            tier,
            json.dumps(evidence),
        )


async def queue_pair(conn, *, primary_id: int, merged_id: int, tier: str, evidence: dict) -> None:
    exists = await conn.fetchval(
        "select 1 from candidate_merges where primary_id=$1 and merged_id=$2 and tier=$3",
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
    row = await conn.fetchrow(
        "select * from candidate_merges where id = $1 and status = 'applied'", merge_id
    )
    if row is None:
        return False
    primary_id, merged_id = row["primary_id"], row["merged_id"]
    async with conn.transaction():
        await conn.execute(
            "update raw_profiles set candidate_id = $2 "
            "where candidate_id = $1 and source = 'linkedin'",
            primary_id,
            merged_id,
        )
        await conn.execute(
            "update candidate_emails set candidate_id = $2 "
            "where candidate_id = $1 and source = 'linkedin'",
            primary_id,
            merged_id,
        )
        # Both rows carry a blob describing evidence they no longer hold.
        await conn.execute(
            """
            update candidates
               set source = 'github',
                   embed_blob = null, embedding = null, embedded_at = null
             where id = $1
            """,
            primary_id,
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
    group.add_argument("--revert", type=int, metavar="MERGE_ID")
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
