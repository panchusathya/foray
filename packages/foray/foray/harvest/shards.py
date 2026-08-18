"""Generate the harvest shard grid.

The GitHub Search API returns at most 1,000 results per query, so the query
space is partitioned into a cross-product of shards each expected to yield under
1,000. ``created`` (account year) is the last dimension because it is the only
one that subdivides cleanly when a shard is still truncated (see search.py).

    40 metros x 12 languages x 6 follower buckets x 11 years = 31,680 shards
"""

from __future__ import annotations

import argparse
import asyncio

from ..config import settings
from ..db import close_pool, get_pool
from ..log import get_logger

log = get_logger("harvest.shards")

LANGUAGES = [
    "python",
    "go",
    "rust",
    "typescript",
    "javascript",
    "java",
    "kotlin",
    "swift",
    "scala",
    "ruby",
    "elixir",
    "cpp",
]

FOLLOWER_BUCKETS = ["10..24", "25..49", "50..99", "100..249", "250..999", ">=1000"]

YEARS = list(range(2012, 2023))  # 2012..2022 inclusive (11 years)


def load_metros() -> list[str]:
    path = settings.data_dir / "metros.txt"
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def created_range_for_year(year: int) -> str:
    return f"{year}-01-01..{year}-12-31"


def compose_query(location: str, language: str, followers: str, created_range: str) -> str:
    return (
        f'location:"{location}" language:{language} '
        f"followers:{followers} created:{created_range} type:user"
    )


def generate_shards() -> list[tuple[str, str, str, str, str]]:
    """Return (location, language, followers, created_range, query) tuples."""
    shards: list[tuple[str, str, str, str, str]] = []
    for loc in load_metros():
        for lang in LANGUAGES:
            for followers in FOLLOWER_BUCKETS:
                for year in YEARS:
                    created_range = created_range_for_year(year)
                    shards.append(
                        (
                            loc,
                            lang,
                            followers,
                            created_range,
                            compose_query(loc, lang, followers, created_range),
                        )
                    )
    return shards


async def _insert(shards: list[tuple[str, str, str, str, str]]) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """insert into harvest_shards (location, language, followers, created_range, query)
               values ($1, $2, $3, $4, $5)
               on conflict (location, language, followers, created_range) do nothing""",
            shards,
        )
        return await conn.fetchval("select count(*) from harvest_shards")


async def amain(args: argparse.Namespace) -> int:
    shards = generate_shards()
    if args.dry_run:
        preview = shards[: args.limit] if args.limit else shards[:20]
        for shard in preview:
            print(shard[4])
        print(f"[dry-run] would generate {len(shards)} shards")
        return 0
    total = await _insert(shards)
    log.info("shards.generated", requested=len(shards), total_in_db=total)
    print(f"generated {len(shards)} shards; harvest_shards now has {total} rows")
    await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the GitHub harvest shard grid.")
    ap.add_argument("--dry-run", action="store_true", help="print query strings, write nothing")
    ap.add_argument("--limit", type=int, default=0, help="dry-run: how many queries to print")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
