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
from ..vocab import GITHUB_SEARCH_LANGUAGES

log = get_logger("harvest.shards")

# Deprecated re-export: vocab.py is now the single source of truth for
# language naming (this list used to live here in GitHub-qualifier spelling,
# and api/parse.py matching queries against it is how "c++" searches went
# dead — derive stores "c++", shard queries need "cpp"). Kept as LANGUAGES so
# existing importers (tests, ad-hoc scripts) keep working; new code should
# import from foray.vocab and pick the spelling it actually needs.
LANGUAGES = list(GITHUB_SEARCH_LANGUAGES)

FOLLOWER_BUCKETS = ["10..24", "25..49", "50..99", "100..249", "250..999", ">=1000"]

YEARS = list(range(2012, 2023))  # 2012..2022 inclusive (11 years)

# Engineer-weighted subset: languages the archetypes actually score, more
# recent accounts, and follower floors that correlate with public git history.
# The full grid stays the default; `--engineer-weighted` is how nightly
# harvest grows the pool that Phase 1/3 can mine.
ENGINEER_LANGUAGES = [
    "python",
    "go",
    "rust",
    "typescript",
    "javascript",
    "java",
    "kotlin",
    "cpp",
]
ENGINEER_FOLLOWER_BUCKETS = ["25..49", "50..99", "100..249", "250..999", ">=1000"]
ENGINEER_YEARS = list(range(2015, 2023))



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


def generate_shards(
    *,
    languages: list[str] | None = None,
    follower_buckets: list[str] | None = None,
    years: list[int] | None = None,
    locations: list[str] | None = None,
) -> list[tuple[str, str, str, str, str]]:
    """Return (location, language, followers, created_range, query) tuples."""
    langs = languages if languages is not None else LANGUAGES
    buckets = follower_buckets if follower_buckets is not None else FOLLOWER_BUCKETS
    year_list = years if years is not None else YEARS
    metros = locations if locations is not None else load_metros()
    shards: list[tuple[str, str, str, str, str]] = []
    for loc in metros:
        for lang in langs:
            for followers in buckets:
                for year in year_list:
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
    if args.engineer_weighted:
        shards = generate_shards(
            languages=ENGINEER_LANGUAGES,
            follower_buckets=ENGINEER_FOLLOWER_BUCKETS,
            years=ENGINEER_YEARS,
        )
    else:
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
    ap.add_argument(
        "--engineer-weighted",
        action="store_true",
        help="restrict to archetype languages, 2015+, followers 25+ (smaller grid)",
    )
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
