"""Seed synthetic, GitHub-shaped raw_profiles for offline development.

This fabricates realistic (fake) candidates so the derive -> embed -> score ->
search pipeline can be exercised end-to-end without a GITHUB_TOKEN or spend.
It is NOT a real export (guardrail-safe): every login is ``synth_*`` and every
field is generated deterministically from a fixed seed.

    uv run python scripts/seed_synthetic.py --count 300 --reset
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from datetime import UTC, datetime, timedelta

from foray.db import close_pool, get_pool

# Repo "themes" drawn from the domain-marker vocabularies so markers + gates fire.
THEMES = {
    "ml": {"lang": "Python", "extra": "Cuda", "terms": ["pytorch", "transformers", "torch"]},
    "gpu": {"lang": "Cuda", "extra": "C++", "terms": ["cuda", "triton", "vllm"]},
    "infra": {"lang": "Go", "extra": "HCL", "terms": ["kubernetes", "terraform", "envoy"]},
    "sre": {"lang": "Go", "extra": "Python", "terms": ["prometheus", "grafana", "opentelemetry"]},
    "db": {"lang": "Rust", "extra": "C++", "terms": ["rocksdb", "clickhouse", "raft"]},
    "data": {"lang": "Scala", "extra": "Python", "terms": ["spark", "airflow", "kafka"]},
    "frontend": {"lang": "TypeScript", "extra": "CSS", "terms": ["react", "vite", "tailwind"]},
    "security": {"lang": "Go", "extra": "Python", "terms": ["fuzzing", "sast", "semgrep"]},
    "mobile": {"lang": "Swift", "extra": "Kotlin", "terms": ["swiftui", "xcode", "coredata"]},
    "backend": {"lang": "Go", "extra": "Python", "terms": ["grpc", "microservices", "postgres"]},
}

COMPANIES = ["@stripe", "@google", "@databricks", "", "", "@openai", "@cloudflare", "", "acme-labs"]
FIRST = ["Ada", "Lin", "Sam", "Noor", "Kai", "Ravi", "Mei", "Ivan", "Zoe", "Omar"]
LAST = ["Kaur", "Rossi", "Novak", "Chen", "Silva", "Khan", "Park", "Diaz", "Ito", "Nguyen"]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_node(rng: random.Random, i: int) -> dict:
    theme_names = rng.sample(list(THEMES), k=rng.choice([1, 1, 2]))
    created_year = rng.randint(2012, 2021)
    created = datetime(created_year, rng.randint(1, 12), rng.randint(1, 28), tzinfo=UTC)
    now = datetime.now(UTC)
    recent_push = now - timedelta(days=rng.randint(5, 500))

    repos = []
    lang_bytes: dict[str, int] = {}
    for t_i, name in enumerate(theme_names):
        theme = THEMES[name]
        stars = rng.choice([2, 8, 30, 120, 800, 2500]) if t_i == 0 else rng.choice([1, 5, 40])
        primary = theme["lang"]
        edges = [
            {"size": rng.randint(40000, 120000), "node": {"name": primary}},
            {"size": rng.randint(5000, 30000), "node": {"name": theme["extra"]}},
        ]
        for edge in edges:
            lang_bytes[edge["node"]["name"]] = lang_bytes.get(edge["node"]["name"], 0) + edge["size"]
        repos.append(
            {
                "name": f"{name}-{rng.choice(['engine', 'kit', 'core', 'lab'])}",
                "description": f"A {name} project using {', '.join(theme['terms'])}.",
                "stargazerCount": stars,
                "forkCount": stars // 10,
                "pushedAt": _iso(recent_push),
                "createdAt": _iso(created + timedelta(days=rng.randint(0, 400))),
                "isArchived": rng.random() < 0.1,
                "primaryLanguage": {"name": primary},
                "repositoryTopics": {"nodes": [{"topic": {"name": term}} for term in theme["terms"]]},
                "languages": {"edges": edges},
            }
        )

    name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    return {
        "name": name,
        "login": f"synth_{i:05d}",
        "email": rng.choice(["", f"user{i}@acmecorp.com", "user@gmail.com"]),
        "company": rng.choice(COMPANIES),
        "location": rng.choice(
            ["San Francisco, CA", "Seattle, WA", "New York, NY", "Austin, TX", "Remote, US"]
        ),
        "bio": f"{theme_names[0]} engineer",
        "websiteUrl": "",
        "twitterUsername": "",
        "createdAt": _iso(created),
        "followers": {"totalCount": rng.choice([12, 40, 120, 900, 3000])},
        "following": {"totalCount": rng.randint(0, 200)},
        "repositories": {"nodes": repos},
        "organizations": {"nodes": []},
        "contributionsCollection": {
            "totalCommitContributions": rng.randint(50, 2000),
            "totalPullRequestContributions": rng.randint(5, 400),
            "restrictedContributionsCount": rng.randint(0, 200),
        },
    }


async def amain(args: argparse.Namespace) -> int:
    rng = random.Random(20240917)
    pool = await get_pool()
    labels: list[dict] = []
    async with pool.acquire() as conn:
        if args.reset:
            await conn.execute(
                "truncate candidates, raw_profiles, candidate_signals, archetype_scores, "
                "shortlist_events restart identity cascade"
            )
        if args.source == "linkedin":
            await seed_linkedin(conn, rng, args.count)
            summary = f"seeded {args.count} synthetic LinkedIn candidates + raw_profiles"
        else:
            summary, labels = await _seed_github(conn, rng, args.count)
    # Outside the acquire: close_pool() waits for every connection to be
    # released, so calling it while `conn` is still held deadlocks forever.
    if args.write_labels:
        from pathlib import Path

        Path(args.write_labels).write_text(
            "\n".join(json.dumps(row) for row in labels) + "\n", encoding="utf-8"
        )
        print(f"wrote labels to {args.write_labels}")
    print(summary)
    await close_pool()
    return 0


async def _seed_github(conn, rng: random.Random, count: int) -> tuple[str, list[dict]]:
    labels: list[dict] = []
    for i in range(count):
        node = make_node(rng, i)
        labels.append({"login": node["login"], "bio": node["bio"]})
        cid = await conn.fetchval(
            "insert into candidates (login, github_id, name, location_raw, discovery_shard) "
            "values ($1, $2, $3, $4, 'synthetic') "
            "on conflict (login) do update set name = excluded.name returning id",
            node["login"],
            1_000_000 + i,
            node["name"],
            node["location"],
        )
        await conn.execute(
            "insert into raw_profiles (candidate_id, source, login, data, fetched_at) "
            "values ($1, 'github', $2, $3::jsonb, now()) "
            "on conflict (candidate_id, source) do update set data = excluded.data, fetched_at = now()",
            cid,
            node["login"],
            json.dumps(node),
        )
    return f"seeded {count} synthetic candidates + raw_profiles", labels


def make_linkedin_record(rng: random.Random, i: int) -> dict:
    """A Bright Data-shaped record, for CI/E2E fixtures — real data never
    reaches a runner, which is exactly why this mode exists."""
    theme_names = rng.sample(list(THEMES), k=1)
    theme = THEMES[theme_names[0]]
    first = FIRST[i % len(FIRST)]
    last = LAST[(i * 7) % len(LAST)]
    slug = f"{first.lower()}-{last.lower()}-synth{i:05d}"
    start_year = rng.randint(2012, 2023)
    title = rng.choice(
        [
            f"Senior {theme_names[0].title()} Engineer",
            f"Staff Software Engineer, {theme_names[0]}",
            "Software Engineer",
            f"{theme_names[0].title()} Engineer",
        ]
    )
    company = rng.choice(["Stripe", "Databricks", "Cloudflare", "Acme Labs", "OpenAI"])
    return {
        "id": slug,
        "linkedin_id": slug,
        "linkedin_num_id": str(90_000_000 + i),
        "url": f"https://www.linkedin.com/in/{slug}",
        "name": f"{first} {last}",
        "first_name": first,
        "last_name": last,
        "city": "San Francisco Bay Area",
        "country_code": "US",
        "position": title,
        "about": f"{title} working on {', '.join(theme['terms'])}.",
        "current_company": {"company_id": company.lower(), "name": company},
        "current_company_name": company,
        "experience": [
            {
                "title": title,
                "company": company,
                "start_date": f"Jan {start_year + 2}",
                "end_date": None,
                "positions": None,
            },
            {
                "title": "Software Engineer",
                "company": "Initech",
                "start_date": f"Jun {start_year}",
                "end_date": f"Dec {start_year + 1}",
                "positions": None,
            },
        ],
        "education": [{"title": "State University", "degree": "BSc"}],
        "certifications": None,
        "followers": rng.randint(50, 3000),
        "connections": rng.randint(50, 500),
    }


async def seed_linkedin(conn, rng: random.Random, count: int) -> None:
    from foray.ingest.linkedin_map import to_import_row

    for i in range(count):
        record = make_linkedin_record(rng, i)
        row = to_import_row(record)
        assert row is not None
        cid = await conn.fetchval(
            "insert into candidates (login, name, location_raw, linkedin_url, linkedin_slug, "
            "linkedin_source, source, discovery_shard) "
            "values ($1, $2, $3, $4, $5, 'synthetic', 'linkedin', 'synthetic') "
            "on conflict (login) do update set name = excluded.name returning id",
            row.login,
            row.name,
            row.location_raw,
            row.linkedin_url,
            row.slug,
        )
        await conn.execute(
            "insert into raw_profiles (candidate_id, source, login, data, fetched_at) "
            "values ($1, 'linkedin', $2, $3::jsonb, now()) "
            "on conflict (candidate_id, source) do update set data = excluded.data, fetched_at = now()",
            cid,
            row.login,
            json.dumps(record),
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed synthetic raw_profiles for offline dev.")
    ap.add_argument("--count", type=int, default=300)
    ap.add_argument(
        "--source",
        choices=("github", "linkedin"),
        default="github",
        help="which evidence kind to fabricate",
    )
    ap.add_argument("--reset", action="store_true", help="truncate foray tables first")
    ap.add_argument("--write-labels", help="write login/bio labels as JSONL")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
