"""Derive candidate_signals from the stored raw GraphQL payloads.

This step makes ZERO API calls (mandatory separation): signal logic changes
often and refetching is expensive, so everything is recomputed from
raw_profiles. ``derive_signals(node)`` is pure and unit-tested against a fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from ..db import close_pool, get_pool
from ..log import Heartbeat, get_logger
from ..search_doc import UPDATE_SEARCH_TSV_SQL
from ..seniority import parse_seniority, seniority_from_experience
from ..vocab import canonical_metro
from .employer import infer_employer
from .markers import detect_markers, load_markers

log = get_logger("enrich.derive")

EXPERIENCE_CAP_YEARS = 25.0
OSS_MAINTAINER_STARS = 100


# location_metro's MEANING changes in 013, with no DDL.
#
# It used to be `(record["city"] or "").strip()` for LinkedIn and hardcoded
# NULL for GitHub, and no search path ever read it: the metro filter went to
# `location_raw ilike any(array[...])`, a ScalarArrayOp that cannot use a
# trigram index (measured on prod: 219 ms, 38k buffers, x4 statements per
# request). It is now a CANONICAL METRO KEY for BOTH sources, which is what
# makes an equality / = any() predicate on an indexed column possible.
#
# Both arms must be filled or the facet is a source filter in disguise: a
# metro predicate over a LinkedIn-only column would silently delete the 7,757
# GitHub rows, the way `primary_language = any(...)` deletes the 196,778
# LinkedIn ones today.
#
# The DEPENDENCY, stated: `foray.vocab.canonical_metro(str | None) -> str | None`,
# which owns data/metro_aliases.yaml and is the SAME function the parser calls
# on a query's location span — a stored value and a searched value cannot
# disagree if only one implementation exists. It returns None for an unmappable
# location, and None is a real answer here: an uncanonicalised city string
# stored in a key column would match nothing while looking populated.


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now() -> datetime:
    return datetime.now(UTC)


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


def admission(
    signals: dict[str, Any], *, about_chars: int, role_count: int
) -> tuple[bool, str | None]:
    """Is this row worth indexing at all? -> (search_eligible, ineligible_reason).

    ONE rule, computed HERE, stored on the row. It already existed once, as
    ``(s.evidence_kind = 'github' or s.title is not null)`` inside
    ``score/common.py``'s candidates_to_score — where only the scoring queue
    could see it. Prod shows what that costs: 24,698 LinkedIn rows have no
    title, no about-text and no experience entries, yet each carries a signals
    row, an embedding and a tsvector. Re-implementing the predicate in tsvector
    SQL, embed selection and _filter_clauses is exactly the drift this hoist
    prevents: derive owns the rule, everyone else tests the flag.

    Deliberately generous — this is admission control, not ranking. A row only
    fails when there is nothing for any arm to match on.

    ``about_chars`` / ``role_count`` come from the raw record (the signals dict
    does not carry the about-text or the role list), threaded through by the
    derive functions so this stays a pure function of stated inputs.

    RECONSIDERED at stage 3 and deliberately left alone: ``role_count`` now
    looks derivable from the new ``role_titles`` column, so this could take one
    fewer argument. It is NOT the same number. ``role_titles`` is deduped and
    junk-filtered, so two dated stints under one title collapse to one entry
    and a role whose only title was the SET/LinkedIn phantom collapses to zero
    — either of which would drop a row from ``role_count >= 2`` to below the
    corroboration bar and flip it ineligible. Substituting it would therefore
    change which rows are indexed, silently, in a stage whose whole point is
    that admission is one rule computed in one place. Behaviour unchanged.
    """
    kind = signals.get("evidence_kind")
    title = signals.get("title")

    # Code evidence: a GitHub identity with at least one non-fork repo has a
    # document to match (repo names, descriptions, topics, language) whether or
    # not title is populated — and title is NULL on all 7,757 prod GitHub rows,
    # so requiring it here would delete the entire GitHub population.
    if kind in ("github", "both") and (signals.get("non_fork_repo_count") or 0) >= 1:
        return True, None

    if title is not None:
        # A title alone is a bare string; one corroborating fact means there is
        # a person behind it. Any of these is enough.
        corroborated = (
            signals.get("employer") is not None
            or signals.get("current_role_start_at") is not None
            or about_chars >= 80
            or role_count >= 2
            or bool(signals.get("domain_markers"))
        )
        if corroborated:
            return True, None
        # 'bare_title': a title and nothing else — no employer, no dates, no
        # prose, one role, no markers. Retrievable in principle (the A-weight
        # tsvector has the title) but there is no profile behind it to rank or
        # to show a recruiter.
        return False, "bare_title"

    # From here title is NULL, so the two remaining slugs split on WHY.
    if kind in ("github", "both"):
        # 'no_code_evidence': a GitHub identity whose only claim was code, with
        # zero non-fork repos. The account exists; the evidence does not.
        return False, "no_code_evidence"
    # 'no_title': the 24,698-row population — a LinkedIn record with no title,
    # which for a career-evidence-only row means no searchable claim at all.
    return False, "no_title"


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
    # Account age as an experience proxy STAYS. It is a real signal — a 2011
    # account with repos from 2013 is evidence of a long career, weaker than a
    # dated role but not absent — and every GitHub node has a createdAt, so
    # this path is never NULL. The asymmetry with LinkedIn is deliberate and is
    # the whole point of est_experience_years becoming nullable: LinkedIn has
    # NO proxy of comparable strength (a profile with no dated role tells us
    # nothing about tenure), so it now writes NULL = "unknown" instead of the
    # 0.0 that 134,536 prod rows carry and that reads as "zero years".
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

    # GitHub has NO title (see "title": None below), so the only seniority
    # reading available here is the experience proxy. est_experience is a
    # max() of two floats that default to 0.0, so it is NEVER None on this
    # path — confirmed by construction above — which means seniority_rank is
    # never NULL for a GitHub row and `seniority_rank >= 4` cannot silently
    # delete the GitHub population. That is the entire reason the proxy exists;
    # seniority_source records that it IS a proxy so no consumer mistakes an
    # account-age band for a title.
    proxy_rank = seniority_from_experience(est_experience)

    signals = {
        "employer": employer,
        "employer_source": employer_source,
        "title": None,
        # Canonical for GitHub too, from the free-text profile location. Before
        # this it was hardcoded NULL, so any metro predicate on the column was
        # "and is a LinkedIn row".
        "location_metro": canonical_metro(node.get("location")),
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
        "domain_markers": detect_markers(repos, language_mix, bio=node.get("bio")),
        "oss_maintainer": any(r["stars"] >= OSS_MAINTAINER_STARS for r in repos),
        "top_repos": top_repos,
        "topics": topics,
        # Evidence-agnostic recency: for GitHub it IS the last push.
        "evidence_kind": "github",
        "activity_at": last_push_at,
        "activity_kind": "push" if last_push_at else None,
        "current_role_start_at": None,
        "tenure_months": None,
        "profile_asof": None,
        # --- derived facets (013) ---------------------------------------
        "seniority_rank": proxy_rank,
        # No title, so no management or founder claim can be made. FALSE, not
        # NULL, for is_founder: the column is NOT NULL because "we have no
        # evidence you founded a company" and "you did not found a company"
        # are the same answer for a filter, whereas a three-valued boolean
        # would make `is_founder = false` silently drop unknowns.
        "mgmt_rank": None,
        "is_founder": False,
        "seniority_source": "experience_proxy" if proxy_rank is not None else None,
        # LinkedIn-only facets: empty arrays, never NULL. An empty array is the
        # honest "this source cannot evidence this", and it keeps every
        # array predicate (&&, @>) total instead of needing an IS NULL arm.
        "past_employers": [],
        "role_titles": [],
        "current_titles": [],
        "schools": [],
        "degrees": [],
        "fields_of_study": [],
        # GitHub's profile `location` is free text with no country field, and
        # inferring a country from it is a geocoding problem, not a mapping
        # one — "Cambridge" alone is UK or US and guessing would be worse than
        # NULL. So country_code stays unknown for GitHub, and any country
        # predicate must admit NULL by default or it becomes a source filter.
        "country_code": None,
        # Admission inputs, not DB columns: they exist so admission() can be
        # recomputed after merge_signals without re-reading the raw payloads.
        # A GitHub node has neither about-text nor dated roles.
        "about_chars": 0,
        "role_count": 0,
    }
    eligible, reason = admission(signals, about_chars=0, role_count=0)
    signals["search_eligible"] = eligible
    signals["ineligible_reason"] = reason
    return signals


def derive_linkedin_signals(record: dict, *, asof: datetime) -> dict[str, Any]:
    """The same signals dict shape, built from career evidence instead of code.

    GitHub numerics stay 0 (never faked); title/employer/metro/experience come
    from the profile — LinkedIn is the FIRST real source of ``title``, and role
    dates beat GitHub's account-age proxy, hence high confidence when present.
    Markers run over pseudo-repos from headline/about/roles with the language
    gate off: prose saying "machine learning" is a claim, not a stray .py file.
    """
    from ..ingest.activity import latest_activity
    from ..ingest.linkedin_map import (
        best_title,
        country_code,
        current_role_start,
        current_titles,
        education,
        employer_of,
        experience_years,
        flatten_experience,
        past_employers,
        pseudo_repos,
        role_titles,
        tenure_months,
    )

    roles = flatten_experience(record)
    years, confidence = experience_years(roles, asof=asof)
    role_start = current_role_start(roles)
    # Real activity evidence, or nothing. See the ladder in ingest/activity.py.
    activity = latest_activity(record)
    title = best_title(record, roles)
    schools, degrees, fields_of_study = education(record)

    # Seniority provenance, decided ONCE here so nothing downstream has to
    # guess. If the title says anything at all -- an IC level, a management
    # level, or founder -- that reading is kept verbatim and stamped 'title',
    # INCLUDING the Nones inside it: "Engineering Manager" stores
    # seniority_rank NULL / mgmt_rank 1, and we do NOT patch the missing IC
    # rank in from the proxy, because a row whose two ordinals came from
    # different evidence classes cannot be described by one source column.
    # Only a title that yields NOTHING falls through to the proxy.
    sen_rank, mgmt_rank, is_founder = parse_seniority(title)
    if sen_rank is not None or mgmt_rank is not None or is_founder:
        seniority_source = "title"
    else:
        sen_rank = seniority_from_experience(years)
        seniority_source = "experience_proxy" if sen_rank is not None else None

    signals = {
        "employer": employer_of(record),
        "employer_source": "linkedin" if employer_of(record) else None,
        "title": title,
        # Was `(record["city"] or "").strip()`, a raw free-text city that no
        # search path ever read. Now a canonical key; see the note above.
        "location_metro": canonical_metro(record.get("city")),
        "language_mix": {},
        "primary_language": None,
        "secondary_language": None,
        "total_stars": 0,
        "max_repo_stars": 0,
        "non_fork_repo_count": 0,
        "archived_fraction": 0.0,
        "commits_last_year": 0,
        "prs_last_year": 0,
        "last_push_at": None,
        "account_age_years": 0.0,
        # NULL, not 0.0, when the profile has no dated role. 134,536 prod rows
        # carry est_experience_years = 0 meaning UNKNOWN, and every consumer
        # read it as "zero years of experience" — a >= 3 filter dropped them as
        # juniors and the heuristic scored them as such. Unknown must look
        # unknown; see the account-age comment in derive_signals for why the
        # GitHub path keeps a number here.
        "est_experience_years": years,
        "experience_confidence": confidence,
        "domain_markers": detect_markers(
            pseudo_repos(record, roles), {}, require_language_gate=False
        ),
        "oss_maintainer": False,
        "top_repos": [],
        "topics": [],
        "evidence_kind": "linkedin",
        # activity_at is EVIDENCE OR NULL — never a clock.
        #
        # It used to be `asof`, stamped `activity_kind='snapshot'`. But `asof`
        # is raw_profiles.fetched_at, which linkedin_file.py's _UPSERT_RAW sets
        # to now() on insert AND on conflict-update: it is the import
        # wall-clock, reset by every re-import. Measured on prod:
        # 196,778 / 196,778 LinkedIn rows passed the 24-month recency floor,
        # all stamped 'snapshot' inside one month — the floor in the search
        # filter and in all 15 archetype gates was a no-op for 96% of the
        # corpus. A predicate that everything passes is not a filter.
        #
        # The ladder (ingest/activity.py): the freshest decodable feed URN from
        # activity[].id ('linkedin_activity', ~48.7% of records), else the
        # freshest posts[].created_at ('linkedin_post', ~2.4%), else NULL +
        # NULL kind meaning genuinely UNKNOWN. NULL is not "stale": it is the
        # honest absence that foray.recency's second arm then admits on the
        # strength of profile_asof, which is where the snapshot claim now
        # lives — and only there.
        #
        # Still true from the previous comment, do not undo it: the role's
        # START date must NOT be used as activity_at. That made anyone in the
        # same job for >18 months fail the recency gates, silently excluding
        # exactly the long-tenured seniors a search most wants. Tenure lives in
        # current_role_start_at/tenure_months.
        "activity_at": activity[0] if activity else None,
        "activity_kind": activity[1] if activity else None,
        "current_role_start_at": role_start,
        "tenure_months": tenure_months(roles, asof=asof),
        "profile_asof": asof,
        # --- derived facets (013) ---------------------------------------
        "seniority_rank": sen_rank,
        "mgmt_rank": mgmt_rank,
        "is_founder": is_founder,
        "seniority_source": seniority_source,
        # The career history, computed by flatten_experience since day one and
        # discarded on every one of 196,778 profiles: Role.current was read
        # only by current_role_start, so its complement -- where a person USED
        # to work -- never reached a column and "ex-Google" was unanswerable.
        #
        # STORED AS STATED ("Reddit, Inc.", "MUFG Union Bank, N.A."), NOT
        # canonicalised. That is deliberate for now but it is a decision with a
        # deadline: `employers` (the current-employer facet) canonicalises the
        # query side through vocab.canonical_employer, so the day a
        # `past_employers` predicate ships it must canonicalise BOTH sides or
        # it will miss every row whose stated form differs from the alias --
        # the UCSF-vs-University-of-California problem, one column over. The
        # facet is wave 2 precisely so this gets decided rather than defaulted;
        # store-canonical is the likelier answer, since it makes the array
        # index usable for equality instead of forcing a trigram scan.
        "past_employers": past_employers(roles),
        # `title` is the best ONE title; these are all of them, and the
        # currently-held subset. Different questions, different columns.
        "role_titles": role_titles(roles),
        "current_titles": current_titles(roles),
        # Coverage measured on the export (2,000 records): schools land on
        # 55.1% of records, degrees on 6.5%, fields on 5.2%. Stored anyway --
        # one array each, and a richer export fills them for free -- but see
        # linkedin_map.education: a HARD degree/field predicate on today's data
        # would delete the 94% that never carried the field.
        "schools": schools,
        "degrees": degrees,
        "fields_of_study": fields_of_study,
        # The same field is_target_geo uses to tell the US San Francisco from
        # the San Franciscos in MX/AR/CR. Present on 100% of the sample.
        "country_code": country_code(record),
        # Admission inputs (see derive_signals): not DB columns.
        "about_chars": len((record.get("about") or "").strip()),
        # role_count is now derivable from role_titles, so this is redundant
        # data — but only ALMOST: len(role_titles) counts DISTINCT non-junk
        # titles while role_count counts role ENTRIES, so a person with two
        # dated stints under one title would drop from 2 roles to 1 title and
        # lose their corroborating fact. admission() therefore keeps reading
        # role_count and its behaviour is unchanged; see the note on
        # admission() itself.
        "role_count": len(roles),
    }
    eligible, reason = admission(
        signals, about_chars=signals["about_chars"], role_count=signals["role_count"]
    )
    signals["search_eligible"] = eligible
    signals["ineligible_reason"] = reason
    return signals


def merge_signals(github: dict[str, Any], linkedin: dict[str, Any]) -> dict[str, Any]:
    """One person, both payloads: LinkedIn wins the career facts, GitHub wins
    the code facts, markers union, recency takes the freshest claim."""
    merged = dict(github)
    for key in (
        "employer",
        "employer_source",
        "title",
        "location_metro",
        # LinkedIn is the only source with a country field at all (GitHub's is
        # NULL by construction), so this coalesce is really "take LinkedIn's".
        "country_code",
    ):
        merged[key] = linkedin[key] or github[key]
    # The career facets are LinkedIn's outright, not coalesced: GitHub's are
    # always empty lists, so `or` would work today but would quietly pick the
    # wrong side the moment GitHub grew any of them.
    for key in (
        "past_employers",
        "role_titles",
        "current_titles",
        "schools",
        "degrees",
        "fields_of_study",
    ):
        merged[key] = linkedin[key]
    if linkedin["est_experience_years"]:
        merged["est_experience_years"] = linkedin["est_experience_years"]
        merged["experience_confidence"] = linkedin["experience_confidence"]
    merged["domain_markers"] = sorted(
        set(github["domain_markers"]) | set(linkedin["domain_markers"])
    )
    # Freshest DATED claim wins; the None-filter is what makes that honest.
    # It was already here and is now load-bearing in a way it was not before:
    # the LinkedIn side used to always supply a date (the import clock stamped
    # 'snapshot'), so on a merged row it beat any GitHub push older than the
    # import and every 'both' row read as freshly active. With activity_at
    # evidence-or-NULL, a LinkedIn row with no decodable feed activity
    # contributes nothing here and a stale GitHub push stays visible as stale.
    # Both-None stays None — merged rows are not exempt from "unknown looks
    # unknown", and foray.recency's snapshot arm still admits them.
    candidates_at = [
        (github.get("activity_at"), github.get("activity_kind")),
        (linkedin.get("activity_at"), linkedin.get("activity_kind")),
    ]
    dated = [(at, kind) for at, kind in candidates_at if at is not None]
    if dated:
        merged["activity_at"], merged["activity_kind"] = max(dated, key=lambda x: x[0])
    else:
        merged["activity_at"], merged["activity_kind"] = None, None
    merged["current_role_start_at"] = linkedin["current_role_start_at"]
    merged["tenure_months"] = linkedin["tenure_months"]
    merged["profile_asof"] = linkedin["profile_asof"]
    merged["evidence_kind"] = "both"
    # Seniority: a REAL TITLE READING IS NEVER DOWNGRADED TO A PROXY. The
    # GitHub side always carries a proxy rank (account age is never NULL), so
    # a naive coalesce or a max() would let an account-age band overwrite
    # "Distinguished Engineer" on precisely the rows we know most about --
    # the merged identities. Ordering, not preference: title wins outright.
    if linkedin["seniority_source"] == "title":
        for key in ("seniority_rank", "mgmt_rank", "is_founder", "seniority_source"):
            merged[key] = linkedin[key]
    else:
        # Neither side read a title, so re-run the proxy over the MERGED
        # experience number — which the block above already set to LinkedIn's
        # dated-role figure when it had one. Recomputing beats copying either
        # side's rank, because the input it was computed from has changed.
        rank = seniority_from_experience(merged["est_experience_years"])
        merged["seniority_rank"] = rank
        merged["mgmt_rank"] = None
        merged["is_founder"] = False
        merged["seniority_source"] = "experience_proxy" if rank is not None else None
    # Recompute admission on the MERGED facts rather than OR-ing the two
    # verdicts: evidence_kind is now 'both', and a row can qualify on a
    # combination neither side had alone (LinkedIn's title + GitHub's markers).
    merged["about_chars"] = linkedin.get("about_chars", 0)
    merged["role_count"] = linkedin.get("role_count", 0)
    eligible, reason = admission(
        merged, about_chars=merged["about_chars"], role_count=merged["role_count"]
    )
    merged["search_eligible"] = eligible
    merged["ineligible_reason"] = reason
    return merged


_UPSERT = """
insert into candidate_signals (
    candidate_id, employer, employer_source, title, location_metro,
    language_mix, primary_language, secondary_language,
    total_stars, max_repo_stars, non_fork_repo_count, archived_fraction,
    commits_last_year, prs_last_year, last_push_at,
    account_age_years, est_experience_years, experience_confidence,
    domain_markers, oss_maintainer, top_repos, topics,
    evidence_kind, activity_at, activity_kind,
    current_role_start_at, tenure_months, profile_asof,
    search_eligible, ineligible_reason,
    seniority_rank, mgmt_rank, is_founder, seniority_source,
    past_employers, role_titles, current_titles,
    schools, degrees, fields_of_study, country_code,
    derived_at
) values (
    $1, $2, $3, $4, $5,
    $6::jsonb, $7, $8,
    $9, $10, $11, $12,
    $13, $14, $15,
    $16, $17, $18,
    $19, $20, $21::jsonb, $22,
    $23, $24, $25,
    $26, $27, $28,
    $29, $30,
    $31, $32, $33, $34,
    $35, $36, $37,
    $38, $39, $40, $41,
    now()
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
    evidence_kind = excluded.evidence_kind,
    activity_at = excluded.activity_at,
    activity_kind = excluded.activity_kind,
    current_role_start_at = excluded.current_role_start_at,
    tenure_months = excluded.tenure_months,
    profile_asof = excluded.profile_asof,
    -- Admission is recomputed on every derive, both directions: a re-scrape
    -- that finally lands a title must flip an ineligible row back on (which
    -- is why the purge runbook nulls tsvector/embedding rather than deleting
    -- rows), and a row that loses its title must flip off.
    search_eligible = excluded.search_eligible,
    ineligible_reason = excluded.ineligible_reason,
    -- Derived facets (013). Every one of these is recomputed from
    -- raw_profiles on every derive, so they must all appear here: a column
    -- present in the INSERT list but MISSING from this list would be correct
    -- for new rows and permanently stale for the 204,535 existing ones, which
    -- is the failure mode a re-derive is supposed to fix.
    seniority_rank = excluded.seniority_rank,
    mgmt_rank = excluded.mgmt_rank,
    is_founder = excluded.is_founder,
    seniority_source = excluded.seniority_source,
    past_employers = excluded.past_employers,
    role_titles = excluded.role_titles,
    current_titles = excluded.current_titles,
    schools = excluded.schools,
    degrees = excluded.degrees,
    fields_of_study = excluded.fields_of_study,
    country_code = excluded.country_code,
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
        signals["evidence_kind"],
        signals["activity_at"],
        signals["activity_kind"],
        signals["current_role_start_at"],
        signals["tenure_months"],
        signals["profile_asof"],
        # $29 onward — positional, so the tail must stay in exactly this
        # order, matching the column list, the VALUES placeholders and the DO
        # UPDATE SET list above. A mismatch here does not error: asyncpg would
        # happily write a reason string into a boolean-shaped slot or shift
        # every column after it by one — which is why the four lists were
        # machine-checked against each other (column count vs placeholder
        # count, placeholders sequential 1..N, every column present in DO
        # UPDATE SET, column->arg pairing printed and eyeballed) rather than
        # read carefully. Re-check after ANY edit to either side.
        signals["search_eligible"],
        signals["ineligible_reason"],
        # $31..$41 — the derived facets (013).
        signals["seniority_rank"],
        signals["mgmt_rank"],
        signals["is_founder"],
        signals["seniority_source"],
        signals["past_employers"],
        signals["role_titles"],
        signals["current_titles"],
        signals["schools"],
        signals["degrees"],
        signals["fields_of_study"],
        signals["country_code"],
    )
    # Keep the lean identity columns on candidates current, too — but only from
    # GitHub nodes: a LinkedIn record's name/city were written at import time,
    # and node.get() on it would blank them.
    if signals["evidence_kind"] == "github":
        await conn.execute(
            "update candidates set name = coalesce($1, name), "
            "location_raw = coalesce($2, location_raw) where id = $3",
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

    # Its own acquire, so close_pool() runs after the connection is released.
    # Awaiting close_pool() inside the block below would wait forever for a
    # connection that only the block's exit can return.
    if args.histogram and not args.derive:
        async with pool.acquire() as conn:
            await _print_histogram(conn)
        await close_pool()
        return 0

    async with pool.acquire() as conn:
        # Fetch ids first (small), then pull the heavy JSONB in chunks. The old
        # "select candidate_id, data from raw_profiles" loaded every ~30 KB node
        # into memory at once and OOMed the 4 GB VM around 40-50k profiles;
        # --stale-only also makes an incremental nightly run touch only new work
        # instead of re-deriving the lowest ids every night.
        stale = (
            "and (s.candidate_id is null or s.derived_at < r.fetched_at)"
            if args.stale_only
            else ""
        )
        params: list = []
        if args.source != "all":
            params.append(args.source)
        if args.limit:
            params.append(args.limit)
        source_clause = (
            f"and r.source = ${len(params) - (1 if args.limit else 0)}"
            if args.source != "all"
            else ""
        )
        ids = [
            r["candidate_id"]
            for r in await conn.fetch(
                f"""
                select distinct r.candidate_id from raw_profiles r
                left join candidate_signals s on s.candidate_id = r.candidate_id
                where true {source_clause} {stale}
                order by r.candidate_id
                """
                + (f"limit ${len(params)}" if args.limit else ""),
                *params,
            )
        ]
        CHUNK = 500
        with Heartbeat(log, lambda: {"processed": processed}):
            for start in range(0, len(ids), CHUNK):
                batch = ids[start : start + CHUNK]
                rows = await conn.fetch(
                    "select candidate_id, source, data, fetched_at from raw_profiles "
                    "where candidate_id = any($1::bigint[]) "
                    "order by candidate_id, source",
                    batch,
                )
                # Group per candidate: a merged identity holds BOTH payloads,
                # and deriving from only one would clobber the other's facts.
                by_candidate: dict[int, dict[str, Any]] = {}
                for row in rows:
                    node = row["data"]
                    if isinstance(node, str):
                        node = json.loads(node)
                    by_candidate.setdefault(row["candidate_id"], {})[row["source"]] = (
                        node,
                        row["fetched_at"],
                    )
                for candidate_id, payloads in by_candidate.items():
                    try:
                        github_sig = None
                        linkedin_sig = None
                        node = {}
                        if "github" in payloads:
                            node, _ = payloads["github"]
                            github_sig = derive_signals(node)
                        if "linkedin" in payloads:
                            li_node, fetched_at = payloads["linkedin"]
                            asof = fetched_at or datetime.now(UTC)
                            linkedin_sig = derive_linkedin_signals(li_node, asof=asof)
                            if github_sig is None:
                                node = li_node
                        if github_sig and linkedin_sig:
                            signals = merge_signals(github_sig, linkedin_sig)
                        else:
                            signals = github_sig or linkedin_sig
                        if signals is None:
                            continue
                        await _upsert_signals(conn, candidate_id, node, signals)
                    except Exception as exc:  # noqa: BLE001 - isolate one bad row
                        await conn.execute(
                            "insert into enrich_errors (candidate_id, stage, error) values ($1, 'derive', $2)",
                            candidate_id,
                            str(exc)[:500],
                        )
                        log.error("derive.error", candidate_id=candidate_id, error=str(exc))
                    processed += 1
                await conn.execute(UPDATE_SEARCH_TSV_SQL, batch)
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
        "--source",
        choices=("github", "linkedin", "all"),
        default="all",
        help="restrict the queue to one raw_profiles source",
    )
    ap.add_argument(
        "--stale-only",
        action="store_true",
        help="only candidates with no signals or signals older than their profile",
    )
    ap.add_argument(
        "--histogram",
        "--stats",
        action="store_true",
        help="print marker distribution (flags any marker above 25%)",
    )
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
