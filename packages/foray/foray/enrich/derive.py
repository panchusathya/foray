"""Derive candidate_signals from the stored raw GraphQL payloads.

This step makes ZERO API calls (mandatory separation): signal logic changes
often and refetching is expensive, so everything is recomputed from
raw_profiles. ``derive_signals(node)`` is pure and unit-tested against a fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from ..db import close_pool, get_pool
from ..log import Heartbeat, get_logger
from .employer import infer_employer
from .markers import detect_markers, load_markers

log = get_logger("enrich.derive")

EXPERIENCE_CAP_YEARS = 25.0
OSS_MAINTAINER_STARS = 100


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_repos(node: dict) -> list[dict]:
    repos: list[dict] = []
    for r in (node.get("repositories") or {}).get("nodes") or []:
        if r is None:
            continue
        topics = [
            (t.get("topic") or {}).get("name")
            for t in ((r.get("repositoryTopics") or {}).get("nodes") or [])
        ]
        repos.append(
            {
                "name": r.get("name"),
                "description": r.get("description") or "",
                "stars": r.get("stargazerCount") or 0,
                "forks": r.get("forkCount") or 0,
                "pushed_at": r.get("pushedAt"),
                "created_at": r.get("createdAt"),
                "archived": bool(r.get("isArchived")),
                "primary_language": (r.get("primaryLanguage") or {}).get("name"),
                "topics": [t for t in topics if t],
                "languages": (r.get("languages") or {}).get("edges") or [],
            }
        )
    return repos


def compute_language_mix(repos: list[dict]) -> dict[str, float]:
    totals: dict[str, int] = {}
    for repo in repos:
        for edge in repo.get("languages", []):
            name = (edge.get("node") or {}).get("name")
            size = edge.get("size") or 0
            if name:
                totals[name.lower()] = totals.get(name.lower(), 0) + size
    grand = sum(totals.values())
    if grand == 0:
        return {}
    ordered = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    return {name: round(size / grand, 4) for name, size in ordered}


def _experience_confidence(non_fork: int, account_age_years: float) -> str:
    if non_fork >= 5 and account_age_years >= 3:
        return "high"
    if non_fork >= 1:
        return "medium"
    return "low"


def derive_signals(node: dict) -> dict[str, Any]:
    """Pure: raw GraphQL user node -> a candidate_signals row (no I/O)."""
    repos = normalize_repos(node)
    language_mix = compute_language_mix(repos)
    langs = list(language_mix.keys())

    total_stars = sum(r["stars"] for r in repos)
    max_stars = max((r["stars"] for r in repos), default=0)
    non_fork = len(repos)
    archived_fraction = (
        sum(1 for r in repos if r["archived"]) / non_fork if non_fork else 0.0
    )

    contrib = node.get("contributionsCollection") or {}
    commits_last_year = contrib.get("totalCommitContributions") or 0
    prs_last_year = contrib.get("totalPullRequestContributions") or 0

    push_dates = [_parse_dt(r["pushed_at"]) for r in repos if r["pushed_at"]]
    last_push_at = max((d for d in push_dates if d), default=None)

    now = _now()
    created = _parse_dt(node.get("createdAt"))
    account_age_years = round((now - created).days / 365.25, 2) if created else 0.0
    repo_created = [d for d in (_parse_dt(r["created_at"]) for r in repos) if d]
    years_since_first_repo = (
        round((now - min(repo_created)).days / 365.25, 2) if repo_created else 0.0
    )
    est_experience = min(EXPERIENCE_CAP_YEARS, max(account_age_years, years_since_first_repo))

    top_repos = [
        {
            "name": r["name"],
            "stars": r["stars"],
            "description": r["description"],
            "language": r["primary_language"],
        }
        for r in sorted(repos, key=lambda r: r["stars"], reverse=True)[:5]
    ]

    topics: list[str] = []
    seen: set[str] = set()
    for repo in repos:
        for topic in repo["topics"]:
            key = topic.lower()
            if key not in seen:
                seen.add(key)
                topics.append(topic)
    topics = topics[:20]

    employer, employer_source = infer_employer(node)

    return {
        "employer": employer,
        "employer_source": employer_source,
        "title": None,
        "location_metro": None,
        "language_mix": language_mix,
        "primary_language": langs[0] if langs else None,
        "secondary_language": langs[1] if len(langs) > 1 else None,
        "total_stars": total_stars,
        "max_repo_stars": max_stars,
        "non_fork_repo_count": non_fork,
        "archived_fraction": round(archived_fraction, 4),
        "commits_last_year": commits_last_year,
        "prs_last_year": prs_last_year,
        "last_push_at": last_push_at,
        "account_age_years": account_age_years,
        "est_experience_years": est_experience,
        "experience_confidence": _experience_confidence(non_fork, account_age_years),
        "domain_markers": detect_markers(repos, language_mix),
        "oss_maintainer": any(r["stars"] >= OSS_MAINTAINER_STARS for r in repos),
        "top_repos": top_repos,
        "topics": topics,
    }


_UPSERT = """
insert into candidate_signals (
    candidate_id, employer, employer_source, title, location_metro,
    language_mix, primary_language, secondary_language,
    total_stars, max_repo_stars, non_fork_repo_count, archived_fraction,
    commits_last_year, prs_last_year, last_push_at,
    account_age_years, est_experience_years, experience_confidence,
    domain_markers, oss_maintainer, top_repos, topics, derived_at
) values (
    $1, $2, $3, $4, $5,
    $6::jsonb, $7, $8,
    $9, $10, $11, $12,
    $13, $14, $15,
    $16, $17, $18,
    $19, $20, $21::jsonb, $22, now()
)
on conflict (candidate_id) do update set
    employer = excluded.employer,
    employer_source = excluded.employer_source,
    title = excluded.title,
    location_metro = excluded.location_metro,
    language_mix = excluded.language_mix,
    primary_language = excluded.primary_language,
    secondary_language = excluded.secondary_language,
    total_stars = excluded.total_stars,
    max_repo_stars = excluded.max_repo_stars,
    non_fork_repo_count = excluded.non_fork_repo_count,
    archived_fraction = excluded.archived_fraction,
    commits_last_year = excluded.commits_last_year,
    prs_last_year = excluded.prs_last_year,
    last_push_at = excluded.last_push_at,
    account_age_years = excluded.account_age_years,
    est_experience_years = excluded.est_experience_years,
    experience_confidence = excluded.experience_confidence,
    domain_markers = excluded.domain_markers,
    oss_maintainer = excluded.oss_maintainer,
    top_repos = excluded.top_repos,
    topics = excluded.topics,
    derived_at = now()
"""


async def _upsert_signals(conn, candidate_id: int, node: dict, signals: dict[str, Any]) -> None:
    await conn.execute(
        _UPSERT,
        candidate_id,
        signals["employer"],
        signals["employer_source"],
        signals["title"],
        signals["location_metro"],
        json.dumps(signals["language_mix"]),
        signals["primary_language"],
        signals["secondary_language"],
        signals["total_stars"],
        signals["max_repo_stars"],
        signals["non_fork_repo_count"],
        signals["archived_fraction"],
        signals["commits_last_year"],
        signals["prs_last_year"],
        signals["last_push_at"],
        signals["account_age_years"],
        signals["est_experience_years"],
        signals["experience_confidence"],
        signals["domain_markers"],
        signals["oss_maintainer"],
        json.dumps(signals["top_repos"]),
        signals["topics"],
    )
    # Keep the lean identity columns on candidates current, too.
    await conn.execute(
        "update candidates set name = $1, location_raw = $2 where id = $3",
        node.get("name"),
        node.get("location"),
        candidate_id,
    )


async def _print_histogram(conn) -> None:
    total = await conn.fetchval("select count(*) from candidate_signals")
    if not total:
        print("no candidate_signals yet")
        return
    rows = await conn.fetch(
        "select marker, count(*) as n from ("
        "  select unnest(domain_markers) as marker from candidate_signals"
        ") t group by marker order by n desc"
    )
    print(f"\nmarker distribution across {total} candidates:")
    for row in rows:
        pct = 100.0 * row["n"] / total
        flag = "  <-- >25%, tighten term file" if pct > 25 else ""
        print(f"  {row['marker']:10s} {row['n']:6d}  {pct:5.1f}%{flag}")
    for marker in load_markers():
        if not any(r["marker"] == marker for r in rows):
            print(f"  {marker:10s} {0:6d}  {0.0:5.1f}%")


async def _print_sample(conn, n: int) -> None:
    rows = await conn.fetch(
        "select c.login, s.employer, s.employer_source, s.est_experience_years, "
        "s.experience_confidence, s.primary_language, s.language_mix, s.domain_markers "
        "from candidate_signals s join candidates c on c.id = s.candidate_id "
        "order by random() limit $1",
        n,
    )
    print(f"\n{len(rows)} random derived candidates:")
    for r in rows:
        print(
            f"  {r['login']:20s} employer={r['employer']!r} ({r['employer_source']}) "
            f"exp={r['est_experience_years']}y/{r['experience_confidence']} "
            f"lang={r['primary_language']} markers={list(r['domain_markers'])}"
        )


async def amain(args: argparse.Namespace) -> int:
    pool = await get_pool()
    processed = 0
    async with pool.acquire() as conn:
        if args.histogram and not args.derive:
            await _print_histogram(conn)
            await close_pool()
            return 0

        # Fetch ids first (small), then pull the heavy JSONB in chunks. The old
        # "select candidate_id, data from raw_profiles" loaded every ~30 KB node
        # into memory at once and OOMed the 4 GB VM around 40-50k profiles;
        # --stale-only also makes an incremental nightly run touch only new work
        # instead of re-deriving the lowest ids every night.
        where = (
            "left join candidate_signals s on s.candidate_id = r.candidate_id "
            "where s.candidate_id is null or s.derived_at < r.fetched_at"
            if args.stale_only
            else ""
        )
        ids = [
            r["candidate_id"]
            for r in await conn.fetch(
                f"select r.candidate_id from raw_profiles r {where} "
                f"order by r.candidate_id " + ("limit $1" if args.limit else ""),
                *([args.limit] if args.limit else []),
            )
        ]
        CHUNK = 500
        with Heartbeat(log, lambda: {"processed": processed}):
            for start in range(0, len(ids), CHUNK):
                batch = ids[start : start + CHUNK]
                rows = await conn.fetch(
                    "select candidate_id, data from raw_profiles "
                    "where candidate_id = any($1::bigint[])",
                    batch,
                )
                for row in rows:
                    node = row["data"]
                    if isinstance(node, str):
                        node = json.loads(node)
                    try:
                        signals = derive_signals(node)
                        await _upsert_signals(conn, row["candidate_id"], node, signals)
                    except Exception as exc:  # noqa: BLE001 - isolate one bad row
                        await conn.execute(
                            "insert into enrich_errors (candidate_id, stage, error) values ($1, 'derive', $2)",
                            row["candidate_id"],
                            str(exc)[:500],
                        )
                        log.error("derive.error", candidate_id=row["candidate_id"], error=str(exc))
                    processed += 1
        log.info("derive.done", processed=processed)
        print(f"derived signals for {processed} candidates (0 API calls)")

        if args.histogram:
            await _print_histogram(conn)
        if args.sample:
            await _print_sample(conn, args.sample)
    await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Derive candidate_signals from raw_profiles (no API).")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--stale-only",
        action="store_true",
        help="only candidates with no signals or signals older than their profile",
    )
    ap.add_argument("--histogram", action="store_true", help="print marker distribution")
    ap.add_argument("--sample", type=int, default=0, help="print N random derived candidates")
    ap.add_argument(
        "--no-derive",
        dest="derive",
        action="store_false",
        help="skip derivation (use with --histogram to just report)",
    )
    ap.set_defaults(derive=True)
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
