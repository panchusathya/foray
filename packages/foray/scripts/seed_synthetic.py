"""Seed synthetic, GitHub-shaped raw_profiles for offline development.

This fabricates realistic (fake) candidates so the derive -> embed -> score ->
search pipeline can be exercised end-to-end without a GITHUB_TOKEN or spend.
It is NOT a real export (guardrail-safe): every login is ``synth_*`` and every
field is generated deterministically from a fixed seed.

    uv run python scripts/seed_synthetic.py --count 300 --reset

For the eval corpus use ``--source both``: eval/run_eval.py judges Boolean
facets, and every wave-1 facet sits on a source-partial column, so a
single-source corpus cannot express "this predicate deleted the other
population" — there is no other population in it to lose.
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

# Human-readable form of each theme key, for titles. "Founding Ml Engineer" is
# not a title anyone holds; the seniority ladder below is only useful as an eval
# fixture if the strings look like the ones derive will meet in prod.
THEME_LABELS = {
    "ml": "ML",
    "gpu": "GPU",
    "infra": "Infrastructure",
    "sre": "Reliability",
    "db": "Database",
    "data": "Data",
    "frontend": "Frontend",
    "security": "Security",
    "mobile": "Mobile",
    "backend": "Backend",
}

# Canonical metro key -> the raw CITY strings a Bright Data export actually
# ships for it. The seed writes the CITY, never the key: canonicalisation lives
# in derive/vocab, and a corpus pre-stamped with `sf_bay_area` would bypass the
# one code path every metro facet depends on. Four+ metros because with every
# row in "San Francisco Bay Area" (what this file used to do) a metro query
# matches EVERYONE — the ground truth is the whole corpus and the query cannot
# fail, which is precisely why the harness never caught a metro leak.
#
# Every string here was checked against vocab.canonical_metro and maps to its
# key (the _unmapped_gb entry excepted, deliberately). That check matters: the
# eval reads metro ground truth from the derived column, so a city the alias
# table misses does not fail loudly — it silently shrinks that metro's
# ground-truth set. Two earlier picks, "Jersey City" and "Round Rock", mapped to
# NULL for exactly that reason. Re-run the check when adding a city.
METRO_CITIES = {
    "sf_bay_area": [
        "San Francisco Bay Area",
        "Palo Alto, California",
        "Mountain View, California",
        "Oakland, California",
    ],
    "nyc": ["New York, New York", "Brooklyn, New York", "Manhattan, New York"],
    "seattle": ["Seattle, Washington", "Bellevue, Washington", "Redmond, Washington"],
    "austin": ["Austin, Texas"],
    # Deliberately OUTSIDE the four canonical keys, and the corpus's only
    # non-US country_code. Two paths need it: a metro the vocabulary is not
    # required to map (so location_metro stays NULL and the metro-unknown
    # policy runs on a LinkedIn row rather than only on GitHub rows), and a
    # country facet with something to exclude.
    "_unmapped_gb": ["London, England, United Kingdom"],
}
METRO_KEYS = list(METRO_CITIES)
# Only the exceptions; everything else is US. Keyed on the metro, not drawn, so
# country and city can never disagree on a row.
METRO_COUNTRY = {"_unmapped_gb": "GB"}

# The IC / management / founder ladder, expressed as TITLES — the only thing
# derive gets to read, since seniority_rank / mgmt_rank / is_founder are all
# computed from s.title. The four templates this replaced topped out at
# "Senior", so `seniority_min = staff` matched NOBODY: the filter looked correct
# because nothing in the corpus could contradict it. `{label}` is the theme.
# A None rung is a row with NO TITLE AT ALL (see make_linkedin_record): the
# 24,698-row prod population that admission control is supposed to delete, and
# the seniority-unknown path's only input on the LinkedIn side.
# Ranks below are what foray.seniority.parse_seniority ACTUALLY returns for
# these exact strings, verified rung by rung — not what the wording suggests.
SENIORITY_LADDER = [
    "{label} Engineering Intern",         # rank 1
    "Junior Software Engineer",           # rank 2
    "Associate {label} Engineer",         # rank 2, different wording
    # No level word, so seniority_rank is NULL — and parse.py notes this is the
    # single most common title shape in the corpus. These two rungs are the
    # seniority-UNKNOWN population, which is the one the unknown policy exists
    # for: include_unknown_seniority defaults True precisely so a `>= senior`
    # filter does not delete them. Without them in the corpus that default is
    # untested.
    "Software Engineer",                  # rank NULL
    "{label} Engineer",                   # rank NULL
    "Senior {label} Engineer",            # rank 4
    "Staff Software Engineer, {label}",   # rank 5 — the `>= staff` edge
    "Principal {label} Engineer",         # rank 5 (principal is the IC staff rung)
    "Engineering Manager, {label}",       # mgmt 1, rank NULL — NOT a senior IC,
    "Director of {label} Engineering",    # mgmt 2, rank NULL   the whole reason
    "VP of Engineering",                  # mgmt 3, rank NULL   mgmt_rank exists
    "Founding {label} Engineer",          # is_founder True, rank NULL
    None,                                 # no title (admission control's target)
]

# Current employers. Named constants because eval/queries.yaml asserts on one of
# them (`employer_any: [stripe]`) and a silent rename here would turn that
# query's ground truth into the empty set rather than failing.
EMPLOYERS = ["Stripe", "Databricks", "Cloudflare", "Acme Labs", "OpenAI"]
# Prior employers, so past_employers has spread. "Initech" for every row (the
# old value) makes an employer facet indistinguishable from a constant.
PAST_EMPLOYERS = ["Initech", "Google", "Amazon", "Palantir", "Two Sigma", "Shopify"]

SCHOOLS = [
    "Stanford University",
    "Carnegie Mellon University",
    "University of California, Berkeley",
    "Georgia Institute of Technology",
    "State University",
]
DEGREES = ["BSc", "MS", "PhD", "BEng"]
FIELDS = ["Computer Science", "Electrical Engineering", "Mathematics", "Physics"]

# Non-engineering personas seeded alongside the THEMES engineers so the eval can
# EXPRESS a ranking mistake: with an all-engineer corpus, "personal trainer beats
# ML engineer" (the prod incident) is literally unrepresentable. Each persona is
# tuned to a failure class we actually saw or expect:
#   * trainer / account exec  — pure off-domain prose, should never rank
#   * logistics               — lexical-noise trap: "shipped", "logistics",
#                               "training" collide with engineer query words
#   * project manager         — "infrastructure projects" without any tech
#   * recruiter               — the hardest class: the profile NAMES the tech
#                               ("python developers", "machine learning
#                               engineers") without the person doing any of it
# The last two are FACET-shaped rather than prose-shaped, and they are a
# different failure class from the five above: those are false positives a
# RANKER admits, these are false positives a BOOLEAN FILTER admits. A Senior
# Personal Training Counselor satisfies `seniority_min >= senior` on a literal
# reading of the title (the prod incident, in facet form), and a Technical
# Recruiter at Stripe satisfies `employers = [stripe]` exactly — right company,
# wrong human — while naming technologies in its about-text on purpose so the
# lexical arm helps it along. Both carry the same "-distr" marker, so junk@10
# already counts them and filter_violations@10 can be checked against them.
# Deliberate interplay with ingest: distractors are PEOPLE with junk-job titles.
# Their person names still come from FIRST/LAST (the company strings below —
# incl. the "LLC" one — appear only in company fields), so they pass the
# company-name-in-person-field ingest filter and land in the index. Distractors
# test RANKING, not ingest filtering.
DISTRACTORS = [
    {
        "title": "Personal Training Counselor",
        "company": "LA Fitness",
        "prior_title": "Group Fitness Instructor",
        "prior_company": "24 Hour Fitness",
        "about": (
            "Certified personal training counselor helping fitness clients reach their "
            "goals through customized workout plans, nutrition coaching, and group classes."
        ),
    },
    {
        "title": "Logistics Coordinator",
        "company": "Amerigo Logistics LLC",
        "prior_title": "Warehouse Supervisor",
        "prior_company": "Regional Freight Co",
        # "shipped" / "logistics" / "training" chosen on purpose: they collide
        # lexically with engineer queries ("shipped training infrastructure").
        "about": (
            "Coordinated freight and warehouse operations; shipped over 4,000 pallets a "
            "month, ran forklift safety training, and optimized logistics routes for "
            "regional carriers."
        ),
    },
    {
        "title": "Project Manager",
        "company": "Confidential Company",
        "prior_title": "Project Coordinator",
        "prior_company": "BuildRight Construction",
        "about": (
            "Project manager delivering large infrastructure projects on time and under "
            "budget; stakeholder alignment, risk registers, and weekly status reporting."
        ),
    },
    {
        "title": "Technical Recruiter",
        "company": "TalentBridge Staffing",
        "prior_title": "Sourcing Specialist",
        "prior_company": "Hays",
        # Hardest distractor class: recruiters' profiles NAME the tech verbatim.
        "about": (
            "I source and place python developers, machine learning engineers, and "
            "kubernetes platform specialists at high-growth startups. Always hiring — "
            "reach out if you write pytorch or terraform for a living!"
        ),
    },
    {
        "title": "Account Executive",
        "company": "Salesforce",
        "prior_title": "Sales Development Representative",
        "prior_company": "Oracle",
        "about": (
            "Enterprise account executive consistently exceeding quota; pipeline "
            "generation, discovery calls, and multi-year SaaS renewals."
        ),
    },
    {
        # Facet-shaped: "Senior" is a real seniority word in a real title, so a
        # seniority filter that reads the title as a bag of words admits this
        # row at rank 1. Kept ALONGSIDE the plain "Personal Training Counselor"
        # above rather than replacing it — the plain one tests the ranker, this
        # one tests the filter, and losing either loses a distinct regression.
        "title": "Senior Personal Training Counselor",
        "company": "LA Fitness",
        "prior_title": "Personal Training Manager",
        "prior_company": "Equinox",
        "about": (
            "Senior personal training counselor leading a team of trainers; built our "
            "onboarding program, shipped a new client assessment process, and ran "
            "quarterly staff training for six locations."
        ),
    },
    {
        # Facet-shaped: an employer facet on Stripe pulls this in exactly, and
        # the about-text names the technologies on purpose. `employer_any` and
        # `titles` disagree about this row, which is the point.
        "title": "Technical Recruiter",
        "company": "Stripe",
        "prior_title": "Technical Sourcer",
        "prior_company": "Robert Half",
        "about": (
            "Technical recruiter at Stripe hiring senior and staff engineers across "
            "payments infrastructure. I place people who write python, go, kubernetes "
            "and pytorch — I do not write any of it myself."
        ),
    },
]

# Fraction of synthetic records (both sources) that are distractors. Drawn from
# the run's single seeded rng so the corpus stays a pure function of the seed.
DISTRACTOR_RATE = 0.30


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_node(rng: random.Random, i: int) -> dict:
    created_year = rng.randint(2012, 2021)
    created = datetime(created_year, rng.randint(1, 12), rng.randint(1, 28), tzinfo=UTC)
    now = datetime.now(UTC)
    recent_push = now - timedelta(days=rng.randint(5, 500))

    repos = []
    # GitHub junk looks different from LinkedIn junk: not a wrong PROFESSION but a
    # non-professional account — dotfiles + interview grind, zero domain terms. The
    # "_distr" login suffix (like the LinkedIn "-distr" slug) marks them for the eval.
    distractor = rng.random() < DISTRACTOR_RATE
    if distractor:
        theme_names = ["distractor"]
        for repo_name, desc in [
            ("dotfiles", "My personal dotfiles and editor config."),
            ("leetcode-practice", "Solutions to coursework and interview practice problems."),
        ]:
            repos.append(
                {
                    "name": repo_name,
                    "description": desc,
                    "stargazerCount": rng.choice([0, 1, 3]),
                    "forkCount": 0,
                    "pushedAt": _iso(recent_push),
                    "createdAt": _iso(created + timedelta(days=rng.randint(0, 400))),
                    "isArchived": False,
                    "primaryLanguage": {"name": rng.choice(["Shell", "Lua", "Java"])},
                    "repositoryTopics": {"nodes": []},
                    "languages": {"edges": [{"size": rng.randint(1000, 9000), "node": {"name": "Shell"}}]},
                }
            )
    else:
        theme_names = rng.sample(list(THEMES), k=rng.choice([1, 1, 2]))
        for t_i, name in enumerate(theme_names):
            theme = THEMES[name]
            stars = rng.choice([2, 8, 30, 120, 800, 2500]) if t_i == 0 else rng.choice([1, 5, 40])
            primary = theme["lang"]
            edges = [
                {"size": rng.randint(40000, 120000), "node": {"name": primary}},
                {"size": rng.randint(5000, 30000), "node": {"name": theme["extra"]}},
            ]
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
                    "repositoryTopics": {
                        "nodes": [{"topic": {"name": term}} for term in theme["terms"]]
                    },
                    "languages": {"edges": edges},
                }
            )

    name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    return {
        "name": name,
        "login": f"synth_{i:05d}_distr" if distractor else f"synth_{i:05d}",
        "email": rng.choice(["", f"user{i}@acmecorp.com", "user@gmail.com"]),
        "company": rng.choice(COMPANIES),
        # Same metro vocabulary as the LinkedIn side so a metro query is not
        # secretly a source query — but note derive_signals hardcodes
        # location_metro = None for GitHub rows, so this string only ever reaches
        # candidates.location_raw. That asymmetry is deliberately left visible:
        # a strict metro facet deletes the entire GitHub population, and the
        # corpus has to be able to show that.
        "location": METRO_CITIES[METRO_KEYS[i % len(METRO_KEYS)]][0],
        # Distractor bios carry no domain terms and no "engineer" — junk accounts
        # rarely self-describe; the "_distr" login is the eval marker, not the bio.
        "bio": "dotfiles and interview prep" if distractor else f"{theme_names[0]} engineer",
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
        parts: list[str] = []
        # `both` exists for the eval, not for convenience. Every facet in wave 1
        # sits on a source-PARTIAL column (title NULL on all 7,757 GitHub rows,
        # primary_language/topics NULL on all 196,778 LinkedIn rows), so a
        # predicate over one of them silently deletes the other population. A
        # single-source corpus cannot express that failure at all: there is no
        # other population to lose. Both arms draw from the same rng in a fixed
        # order, so the corpus stays a pure function of the seed.
        if args.source in ("linkedin", "both"):
            await seed_linkedin(conn, rng, args.count)
            parts.append(f"{args.count} LinkedIn")
        if args.source in ("github", "both"):
            _, labels = await _seed_github(conn, rng, args.count)
            parts.append(f"{args.count} GitHub")
        summary = f"seeded {' + '.join(parts)} synthetic candidates + raw_profiles"
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
    first = FIRST[i % len(FIRST)]
    last = LAST[(i * 7) % len(LAST)]
    slug = f"{first.lower()}-{last.lower()}-synth{i:05d}"
    start_year = rng.randint(2012, 2023)

    # Structural facets come from `i`; only incidental variation comes from rng.
    # WHY: `i` guarantees COVERAGE — every ladder rung and every metro is
    # present for any --count >= len(SENIORITY_LADDER) * len(METRO_KEYS) (65) —
    # whereas rng.choice leaves "was the staff rung seeded at all?" to luck, and
    # an empty ground-truth set reports nDCG 0.000, which reads as a ranking
    # regression rather than as a missing fixture. The strides are the ladder
    # length so the two facets sweep independently instead of correlating.
    rung = SENIORITY_LADDER[i % len(SENIORITY_LADDER)]
    metro_key = METRO_KEYS[(i // len(SENIORITY_LADDER)) % len(METRO_KEYS)]
    city = rng.choice(METRO_CITIES[metro_key])

    # Education variety with holes in it: `i % 4` / `i % 5` leave ~25% of rows
    # with no degree and ~20% with no field, deterministically, so the
    # unknown-degree / unknown-field branches are exercised at any --count
    # rather than only on a corpus that happens to contain a gap. Bright Data's
    # education entries use `title` / `degree` / `field`.
    education: dict = {"title": SCHOOLS[(i * 3) % len(SCHOOLS)]}
    if i % 4:
        education["degree"] = DEGREES[i % len(DEGREES)]
    if i % 5:
        education["field"] = FIELDS[i % len(FIELDS)]

    # ~30% distractors, drawn from the same seeded rng as everything else so the
    # corpus stays deterministic. The "-distr" slug suffix is the eval's ground-
    # truth marker: to_import_row keeps the slug verbatim (login = "li:"+slug),
    # so it survives the whole pipeline where an about-text tag would not.
    # Person NAME stays first+last (see DISTRACTORS comment): the persona's
    # company strings must only ever appear in company fields, or ingest's
    # company-page filter would eat the record before ranking sees it.
    if rng.random() < DISTRACTOR_RATE:
        persona = rng.choice(DISTRACTORS)
        slug += "-distr"
        title, company, about = persona["title"], persona["company"], persona["about"]
        prior_title, prior_company = persona["prior_title"], persona["prior_company"]
    else:
        theme_name = rng.sample(list(THEMES), k=1)[0]
        theme = THEMES[theme_name]
        title = rung.format(label=THEME_LABELS[theme_name]) if rung else None
        company = rng.choice(EMPLOYERS)
        # The theme's LANGUAGE goes into the prose deliberately. A LinkedIn row
        # names "Python" in its about-text while s.primary_language is NULL on
        # every one of the 196,778 prod LinkedIn rows — so `language = 'python'`
        # matches 550 people out of 204,535 and the recruiter cannot tell. That
        # source-disjointness is only observable if the corpus contains a row
        # that clearly SHOULD match a language facet but structurally cannot.
        about = (
            f"{title or 'Engineer'} working on {', '.join(theme['terms'])}; "
            f"mostly {theme['lang']}."
        )
        prior_title = "Software Engineer"
        prior_company = rng.choice(PAST_EMPLOYERS)
    # A None rung means NO title anywhere: `position` empty AND both experience
    # titles empty, because best_title scans every role (including past ones)
    # and a prior title would quietly re-title the row. Companies stay, so this
    # is a title-less row that an EMPLOYER facet can still reach — admission
    # control should nonetheless keep it out of the index entirely.
    no_title = title is None
    return {
        "id": slug,
        "linkedin_id": slug,
        "linkedin_num_id": str(90_000_000 + i),
        "url": f"https://www.linkedin.com/in/{slug}",
        "name": f"{first} {last}",
        "first_name": first,
        "last_name": last,
        "city": city,
        "country_code": METRO_COUNTRY.get(metro_key, "US"),
        "position": "" if no_title else title,
        "about": about,
        "current_company": {"company_id": company.lower().replace(" ", "-"), "name": company},
        "current_company_name": company,
        "experience": [
            {
                "title": "" if no_title else title,
                "company": company,
                "start_date": f"Jan {start_year + 2}",
                "end_date": None,
                "positions": None,
            },
            # The second, ENDED entry is what past_employers is derived from —
            # without a non-current role the column would be empty on every row
            # and an employer facet could not tell "works at" from "worked at".
            {
                "title": "" if no_title else prior_title,
                "company": prior_company,
                "start_date": f"Jun {start_year}",
                "end_date": f"Dec {start_year + 1}",
                "positions": None,
            },
        ],
        "education": [education],
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
        choices=("github", "linkedin", "both"),
        default="github",
        help="which evidence kind to fabricate ('both' = the eval corpus; see amain)",
    )
    ap.add_argument("--reset", action="store_true", help="truncate foray tables first")
    ap.add_argument("--write-labels", help="write login/bio labels as JSONL")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
