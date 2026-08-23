"""Foray-native relevance harness.

eval/run_eval.py cannot evaluate foray — it bootstraps a juicebox Org. This
harness labels the synthetic THEMES corpus with rules and reports nDCG plus
arm diagnostics (lexical_hits / vector_hits / overlap).

    uv run python scripts/seed_synthetic.py --source both --reset && foray derive
    uv run python eval/run_eval.py
    FORAY_RETRIEVAL_ARMS=score uv run python eval/run_eval.py

Three label streams, deliberately separate: relevance (nDCG/P/R), junk@10
(seeded off-domain personas), and fv@10 — rows returned in spite of a hard
filter the parser compiled to SQL. fv@10 is the only one gated at zero by
default, and it is the only one that can catch a Boolean leak: a filter can
leak entirely while nDCG stays healthy, because the rows it leaks come from the
population the relevance rule already likes. ``total_matched`` is printed per
query as the funnel width — the headline claim of the Boolean-first inversion —
and asserted nowhere.

Seed with ``--source both``: every wave-1 facet sits on a source-partial column,
so a single-source corpus cannot show a predicate deleting the other population.

Kept out of per-push CI the way the `model` marker already is.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import asyncpg
import yaml
from metrics import (
    filter_violations_at_k,
    junk_at_k,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

from foray.api.search import parse_arms, run_search
from foray.db import close_pool, get_pool
from foray.embed.hashing import hashing_embed
from foray.seniority import (
    SENIORITY_DISTINGUISHED,
    SENIORITY_ENTRY,
    SENIORITY_INTERN,
    SENIORITY_MID,
    SENIORITY_SENIOR,
    SENIORITY_STAFF,
)

# The same vocabulary the SQL predicates are built from. Re-deriving either of
# these here would let the eval and the filter disagree about what "nyc" means,
# and the eval would end up measuring its own alias table.
from foray.vocab import canonical_employer, metro_like_patterns

HERE = Path(__file__).resolve().parent

# The IC ladder as names, so queries.yaml reads like a recruiter brief instead
# of like a migration. IMPORTED, not re-typed: foray/seniority.py names "the
# eval's seniority_min label" as one of three layers that order-compare these
# numbers, and a local copy reading `principal: 6` against a derive that stores
# 5 would make `>= principal` quietly unsatisfiable — a green fv@10 on a filter
# returning nothing. If the module is renamed this import fails at startup,
# which is the correct failure: loud beats silently mis-scored.
# Note "principal" is the IC-ladder reading (rank 5, same rung as staff); 6 is
# distinguished / technical fellow.
SENIORITY_RANKS = {
    "intern": SENIORITY_INTERN,
    "entry": SENIORITY_ENTRY,
    "junior": SENIORITY_ENTRY,
    "mid": SENIORITY_MID,
    "senior": SENIORITY_SENIOR,
    "staff": SENIORITY_STAFF,
    "principal": SENIORITY_STAFF,
    "distinguished": SENIORITY_DISTINGUISHED,
    "fellow": SENIORITY_DISTINGUISHED,
}

# Stage-3 columns (migration 013). Split out so a run against a pre-013 database
# degrades to the old label set with a loud line rather than crashing: three
# agents are landing this stage in parallel and an eval that cannot start is an
# eval nobody runs.
_STAGE3_COLUMNS = (
    "s.seniority_rank",
    "s.mgmt_rank",
    "s.is_founder",
    "s.seniority_source",
    "s.past_employers",
    "s.role_titles",
    "s.current_titles",
    "s.schools",
    "s.degrees",
    "s.fields_of_study",
    "s.country_code",
)


def is_distractor(login: str) -> bool:
    """Seeded junk personas (personal trainer, recruiter, logistics, ...) carry a
    "-distr" slug / "_distr" login suffix — the one marker the pipeline cannot
    lose (LinkedIn login is "li:"+slug verbatim; GitHub login is inserted as-is).
    Prose-based detection would be circular: the recruiter distractor NAMES the
    tech on purpose."""
    return login.endswith(("-distr", "_distr"))


def _lower_set(values) -> set[str]:
    return {str(v).strip().lower() for v in (values or []) if str(v).strip()}


def _employers_of(row: dict) -> set[str]:
    """Current employer plus past employers, lowercased.

    Current OR past on purpose: "someone from Stripe" is how the brief is meant,
    and past_employers exists precisely so the facet can reach an ex-employee.
    The loose reading also keeps this metric a LOWER bound on violations, which
    is the right direction for a stream that gates CI at zero.
    """
    return _lower_set([row.get("employer"), *(row.get("past_employers") or [])])


def is_relevant(row: dict, rule: dict) -> bool:
    # Distractors are relevant to NO query by construction — that is their job.
    if row.get("distractor"):
        return False
    themes = set(row.get("themes") or [])
    text = " ".join(
        [
            str(row.get("bio") or ""),
            " ".join(row.get("terms") or []),
            " ".join(row.get("domain_markers") or []),
        ]
    ).lower()
    want = rule.get("theme_any") or []
    if want and not themes.intersection(want):
        return False

    # --- facet rules -------------------------------------------------------
    # These deliberately treat UNKNOWN as not-relevant, the opposite of the
    # filter's unknown-passes policy, and the disagreement is not a bug in
    # either. A hard predicate must admit unknowns or it becomes a source
    # filter in disguise (title is NULL on all 7,757 GitHub rows); ground truth
    # cannot credit a row for a claim its record does not make. So an unknown
    # row is allowed through the filter and still counts against precision —
    # which is exactly the trade the unknown policy is making, now visible.
    floor = rule.get("seniority_min")
    if floor is not None:
        rank = row.get("seniority_rank")
        if rank is None or rank < _rank_of(floor):
            return False

    want = rule.get("employer_any")
    if want and not _lower_set(want) & _employers_of(row):
        return False

    want = rule.get("metro_any")
    if want and (row.get("location_metro") or "").lower() not in _lower_set(want):
        return False

    if rule.get("is_founder") is not None and bool(row.get("is_founder")) != bool(
        rule["is_founder"]
    ):
        return False

    want = rule.get("terms_any") or []
    return not want or any(term in text for term in want)


def _rank_of(value) -> int:
    """A rung name or a raw smallint -> the smallint. Raises on nonsense."""
    if isinstance(value, int):
        return value
    try:
        return SENIORITY_RANKS[str(value).strip().lower()]
    except KeyError:
        raise ValueError(
            f"unknown seniority rung {value!r}; expected one of {sorted(SENIORITY_RANKS)}"
        ) from None


# --------------------------------------------------------------- violations --
# Everything below reads `diagnostics["predicates"]` — ParsedFilter's
# to_predicate_summary(), the dict of facet -> coerced value that ACTUALLY
# became SQL (canonical metro keys, lowercased languages, sanitized LIKE
# bodies, not what the recruiter typed) — and never re-derives intent
# from the query string. That is the whole design: "staff or above ML engineer"
# might parse into no seniority predicate at all, and re-deriving the intent
# here would report a violation the system never promised, while reading the
# summary reports the only thing that matters — the system broke its own filter.


def predicate_summary(diagnostics: dict | None) -> dict:
    """`parsed.to_predicate_summary()` out of diagnostics, or {}.

    The live key is "predicates" (search.py). The rest are tried because Stage 3
    landed in parallel with this file and a rename is cheap to absorb here but
    expensive to notice: a missing / renamed / non-dict value means "no predicate
    ran", which makes fv@10 VACUOUSLY zero. amain therefore warns when this
    comes back empty for every query — a metric that cannot fail is worse than
    a metric that fails.
    """
    diag = diagnostics or {}
    for key in ("predicates", "predicate_summary", "to_predicate_summary", "filters"):
        value = diag.get(key)
        if isinstance(value, dict):
            return value
    return {}


# The other agent owns the summary's key names; these are the spellings that
# mean the same facet. Anything unrecognised is REPORTED (see unknown_facets in
# amain), never skipped silently — a rename that turned fv@10 permanently green
# would be indistinguishable from a fixed system.
_FACET_ALIASES = {
    "metro": "metros",
    "metro_any": "metros",
    "location_metro": "metros",
    "employer": "employers",
    "employer_any": "employers",
    "title": "titles",
    "language": "languages",
    "primary_language": "languages",
    "country": "countries",
    "country_code": "countries",
    "countries_any": "countries",
    "topic": "topics",
    "evidence_kind": "source",
}

# Does a NULL / empty column PASS this facet? Transcribed from the clauses
# _filter_clauses actually emits (foray/api/search.py), which is the only
# authority: to_predicate_summary deliberately omits the policy switches
# (strict_language, include_unknown_*) because they are not predicates, so on a
# real run this table is ALL the eval knows about unknown handling. Each entry
# names the clause it mirrors — if one of those clauses changes, this table is
# wrong and fv@10 starts lying in whichever direction the drift went.
_UNKNOWN_PASSES = {
    # `... or s.title is null` — title is NULL on all 7,757 GitHub rows.
    "titles": True,
    # `s.title is null or not (...)` — the three-valued NOT trap; an exclusion
    # must not delete the rows it cannot read.
    "title_exclude": True,
    # `... or coalesce(cardinality(s.topics), 0) = 0` — GitHub-only column.
    "topics": True,
    # `... or s.primary_language is null` — GitHub-only column.
    "languages": True,
    # include_unknown_seniority DEFAULTS TRUE: seniority_rank is NULL for all
    # GitHub rows and for every title with no level word.
    "seniority_min": True,
    "seniority_max": True,
    # No unknown arm, by design: location text exists on both populations, so a
    # metro predicate is not a source filter, and admitting unknowns would make
    # the recruiter's hardest constraint a no-op.
    "metros": False,
    # No unknown arm: employer is populated on both sources, so NULL is a
    # genuine unknown and admitting it would make "at Stripe" match everyone.
    "employers": False,
    # `s.domain_markers && ...` — detect_markers runs on both sources, so an
    # empty array is a real negative rather than a missing column.
    "markers": False,
    # include_unknown_experience / include_unknown_tenure both DEFAULT FALSE: an
    # absence of dates is not evidence of years served, and the pre-NULL
    # behaviour of storing 0 made every unknown answer "yes" to any `<= N`.
    "experience_min": False,
    "experience_max": False,
    "tenure_min_months": False,
    "tenure_max_months": False,
}

# The switch names that, IF a future to_predicate_summary starts echoing them,
# override the defaults above. Absent from the summary they read as "default",
# which is today's behaviour — so this costs nothing now and starts working the
# day the summary carries them, instead of needing a second pass over this file.
_STRICT_SWITCHES = {
    "languages": ("strict_language",),
    "topics": ("strict_topics",),
    "titles": ("strict_titles",),
    "title_exclude": ("strict_titles",),
}
_INCLUDE_UNKNOWN_SWITCHES = {
    "experience_min": ("include_unknown_experience",),
    "experience_max": ("include_unknown_experience",),
    "tenure_min_months": ("include_unknown_tenure",),
    "tenure_max_months": ("include_unknown_tenure",),
    "seniority_min": ("include_unknown_seniority",),
    "seniority_max": ("include_unknown_seniority",),
}


def _unknown_is_violation(summary: dict, facet: str) -> bool:
    if any(summary.get(key) is True for key in _STRICT_SWITCHES.get(facet, ())):
        return True
    switches = _INCLUDE_UNKNOWN_SWITCHES.get(facet, ())
    if any(summary.get(key) is not None for key in switches):
        return not any(summary.get(key) is True for key in switches)
    return not _UNKNOWN_PASSES.get(facet, True)


def _as_list(value) -> list:
    return list(value) if isinstance(value, list | tuple | set) else [value]


def _check_set(row_key: str):
    """Column-in-a-set facets (language, country) share one shape."""

    def check(row: dict, value, summary: dict, facet: str) -> bool:
        actual = (row.get(row_key) or "").strip().lower()
        if not actual:
            return _unknown_is_violation(summary, facet)
        return actual not in _lower_set(_as_list(value))

    return check


def _check_metros(row: dict, value, summary: dict, facet: str) -> bool:
    """Both arms of the metro predicate, or it reads as a violation.

    _filter_clauses emits `s.location_metro = any(keys) or <location_raw LIKE
    alias>...` — the second arm exists because derive writes NO location_metro
    for the 7,757 GitHub rows, so their raw string is the only location evidence
    they have. Checking only the canonical column would report every one of
    those rows as a violation and pin fv@10 at "leaking" for a metro query that
    is behaving exactly as designed. The alias patterns come from vocab, the
    same source the SQL uses, so the two cannot drift apart.
    """
    wanted = _lower_set(_as_list(value))
    if (row.get("location_metro") or "").strip().lower() in wanted:
        return False
    raw = (row.get("location_raw") or "").strip().lower()
    if raw:
        for key in wanted:
            # metro_like_patterns returns SQL LIKE bodies ('%palo alto%'); the
            # eval only needs the substring inside them.
            if any(pattern.strip("%") in raw for pattern in metro_like_patterns(key)):
                return False
    return _unknown_is_violation(summary, facet) if not raw else True


def _check_seniority(*, floor: bool):
    def check(row: dict, value, summary: dict, facet: str) -> bool:
        rank = row.get("seniority_rank")
        if rank is None:
            return _unknown_is_violation(summary, facet)
        want = _rank_of(value)
        return rank < want if floor else rank > want

    return check


def _check_tenure(*, floor: bool):
    def check(row: dict, value, summary: dict, facet: str) -> bool:
        months = row.get("tenure_months")
        if months is None:
            return _unknown_is_violation(summary, facet)
        return months < int(value) if floor else months > int(value)

    return check


def _check_employers(row: dict, value, summary: dict, facet: str) -> bool:
    """CURRENT employer only — deliberately narrower than _employers_of.

    The predicate is `lower(s.employer) = any(canonical) or lower(s.employer)
    like '%v%'`; past_employers is a wave-2 column and reaches no SQL. So a row
    whose only match is a former employer DOES violate this facet, even though
    the relevance rule above counts it as a fair hit. The two questions are
    genuinely different: "did the filter do what it said" vs "would a recruiter
    want this person".
    """
    actual = (row.get("employer") or "").strip().lower()
    if not actual:
        return _unknown_is_violation(summary, facet)
    for wanted in _lower_set(value):
        canonical = canonical_employer(wanted)
        # Exact on the canonical form (the btree fast path), substring on the
        # values vocab could not resolve — mirroring the exact/fuzzy split.
        if canonical and actual == canonical.lower():
            return False
        if not canonical and wanted in actual:
            return False
    return True


def _check_titles(row: dict, value, summary: dict, facet: str) -> bool:
    title = (row.get("title") or "").lower()
    if not title:
        return _unknown_is_violation(summary, facet)
    # Substring, not equality: the predicate is a trigram/ILIKE OR-chain, so
    # "engineer" is meant to reach "Staff Software Engineer, ML".
    return not any(term in title for term in _lower_set(value))


def _check_title_exclude(row: dict, value, summary: dict, facet: str) -> bool:
    # An exclusion has no unknown problem: a NULL title excludes nothing, and a
    # NULL title can never contain the excluded term.
    title = (row.get("title") or "").lower()
    return any(term in title for term in _lower_set(value))


def _check_array(row_key: str):
    """Array-overlap facets: topics (s.topics) and markers (s.domain_markers)."""

    def check(row: dict, value, summary: dict, facet: str) -> bool:
        have = _lower_set(row.get(row_key))
        if not have:
            return _unknown_is_violation(summary, facet)
        return not (_lower_set(value) & have)

    return check


def _check_experience(*, floor: bool):
    def check(row: dict, value, summary: dict, facet: str) -> bool:
        years = row.get("est_experience_years")
        if years is None:
            return _unknown_is_violation(summary, facet)
        return years < float(value) if floor else years > float(value)

    return check


# is_manager / is_founder are TWO-SIDED: to_predicate_summary includes them
# whenever they are not None, so False is a real predicate here
# (`s.mgmt_rank is null`, `not s.is_founder`) and must not be skipped as falsy
# the way an empty title list is.
_BOOLEAN_FACETS = frozenset({"is_manager", "is_founder"})


def _check_mgmt(row: dict, value, summary: dict, facet: str) -> bool:
    # mgmt_rank NULL means NOT-management, per the column's definition — it is
    # not an unknown, so there is no unknown policy to consult here.
    return (row.get("mgmt_rank") is not None) != bool(value)


def _check_founder(row: dict, value, summary: dict, facet: str) -> bool:
    # boolean not null default false, so likewise no unknown state.
    return bool(row.get("is_founder")) != bool(value)


def _check_source(row: dict, value, summary: dict, facet: str) -> bool:
    # `s.evidence_kind = $n`, a plain equality on a single string — 'both' does
    # NOT satisfy source='github' here, so this must not be generous where the
    # SQL is not. (require_code_evidence is the clause that spells 'both' out.)
    kind = (row.get("evidence_kind") or "").lower()
    return bool(kind) and kind not in _lower_set(_as_list(value))


def _check_code_evidence(row: dict, value, summary: dict, facet: str) -> bool:
    return bool(value) and (row.get("evidence_kind") or "") not in ("github", "both")


_FACET_CHECKS = {
    "metros": _check_metros,
    "languages": _check_set("primary_language"),
    "countries": _check_set("country_code"),
    "employers": _check_employers,
    "titles": _check_titles,
    "title_exclude": _check_title_exclude,
    "topics": _check_array("terms"),
    "markers": _check_array("domain_markers"),
    "seniority_min": _check_seniority(floor=True),
    "seniority_max": _check_seniority(floor=False),
    "tenure_min_months": _check_tenure(floor=True),
    "tenure_max_months": _check_tenure(floor=False),
    "experience_min": _check_experience(floor=True),
    "experience_max": _check_experience(floor=False),
    "is_manager": _check_mgmt,
    "is_founder": _check_founder,
    "source": _check_source,
    "require_code_evidence": _check_code_evidence,
}
# Summary keys that are switches or bookkeeping, not predicates over a row.
# Listed so they do not show up as "unknown facet" noise.
_NOT_A_FACET = frozenset(
    {
        *(k for keys in _STRICT_SWITCHES.values() for k in keys),
        *(k for keys in _INCLUDE_UNKNOWN_SWITCHES.values() for k in keys),
        "include_unknown_experience",
        "free_text",
        "parser",
        "parser_version",
        # archetype_id is a gate, not a facet: it selects a whole SQL gate
        # clause rather than one column, and the gates are kept until the
        # parsed facets are as selective (plan §15). Violations of a gate are
        # not expressible from these labels, so it is excluded rather than
        # reported as an unchecked facet.
        "archetype_id",
        "vocab_hash",
        "query",
    }
)


def facet_violations(row: dict, summary: dict, unknown: set[str]) -> list[str]:
    """Facets in `summary` that this row violates. Empty list = clean row."""
    bad: list[str] = []
    for raw_name, value in (summary or {}).items():
        name = _FACET_ALIASES.get(raw_name, raw_name)
        if name in _NOT_A_FACET:
            continue
        check = _FACET_CHECKS.get(name)
        if check is None:
            unknown.add(raw_name)
            continue
        # Falsy means "no predicate ran" — an empty titles list emits no clause.
        # Excepting the two-sided booleans, where False IS the predicate.
        if name not in _BOOLEAN_FACETS and (
            value is None
            or value is False
            or (isinstance(value, list | tuple | set | dict) and not value)
        ):
            continue
        if name in _BOOLEAN_FACETS and value is None:
            continue
        if check(row, value, summary, name):
            bad.append(name)
    return bad


async def collect_labels(conn) -> dict[str, dict]:
    # LinkedIn-seeded logins are "li:{first}-{last}-synthNNNNN[-distr]" — the old
    # 'synth_%'-only filter plus the github-only raw_profiles join left the whole
    # LinkedIn population unlabeled, which is exactly how the personal-trainer
    # incident sailed past the eval. Labels are source-agnostic now: themes/terms
    # come from candidate_signals either way, prose from whichever raw row exists.
    # The facet columns come from the DB rather than from a seed-side sidecar on
    # purpose: ground truth for "is this row in the sf_bay_area" must be what
    # derive actually canonicalised, not what the seed intended. The cost is that
    # a canonicalisation bug shows up as an EMPTY relevant set rather than as a
    # failed query, which is why amain warns loudly on a zero-relevant query.
    base = [
        "c.login",
        "c.name",
        # location_raw, not just s.location_metro: the metro predicate has a
        # second arm over the raw string (see _check_metros).
        "c.location_raw",
        "s.domain_markers",
        "s.topics",
        "s.title",
        "s.employer",
        "s.location_metro",
        "s.evidence_kind",
        "s.tenure_months",
        "s.est_experience_years",
        "s.primary_language",
        "s.search_eligible",
        "coalesce(gh.data->>'bio', li.data->>'about') as bio",
    ]

    def query_for(columns: list[str]) -> str:
        return f"""
        select {", ".join(columns)}
        from candidates c
        join candidate_signals s on s.candidate_id = c.id
        left join raw_profiles gh on gh.candidate_id = c.id and gh.source = 'github'
        left join raw_profiles li on li.candidate_id = c.id and li.source = 'linkedin'
        where (c.login like 'synth_%' or c.login like 'li:%synth%')
          and c.merged_into is null
        """

    try:
        rows = await conn.fetch(query_for([*base, *_STAGE3_COLUMNS]))
    except asyncpg.exceptions.UndefinedColumnError as exc:
        # Pre-013 database. Degrade instead of dying: every facet metric below
        # then reads None and reports nothing, which the printed banner says.
        print(f"WARN migration 013 columns missing ({exc}); facet labels are all NULL")
        rows = await conn.fetch(query_for(base))

    out = {}
    for row in rows:
        markers = list(row["domain_markers"] or [])
        bio = row["bio"] or ""
        record = dict(row)
        out[row["login"]] = {
            "login": row["login"],
            "themes": markers,
            "terms": list(row["topics"] or []),
            "bio": bio,
            "domain_markers": markers,
            "distractor": is_distractor(row["login"]),
            # Facet labels. .get() throughout so the pre-013 fallback above
            # yields None rather than KeyError; a None facet is "unknown", which
            # every checker already has a documented policy for.
            "title": record.get("title"),
            "employer": record.get("employer"),
            "location_metro": record.get("location_metro"),
            "location_raw": record.get("location_raw"),
            "evidence_kind": record.get("evidence_kind"),
            "tenure_months": record.get("tenure_months"),
            "est_experience_years": record.get("est_experience_years"),
            "primary_language": record.get("primary_language"),
            "search_eligible": record.get("search_eligible"),
            "seniority_rank": record.get("seniority_rank"),
            "mgmt_rank": record.get("mgmt_rank"),
            "is_founder": record.get("is_founder"),
            "seniority_source": record.get("seniority_source"),
            "past_employers": list(record.get("past_employers") or []),
            "schools": list(record.get("schools") or []),
            "degrees": list(record.get("degrees") or []),
            "fields_of_study": list(record.get("fields_of_study") or []),
            "country_code": record.get("country_code"),
        }
    return out


_KNOWN_RULE_KEYS = frozenset(
    {"theme_any", "terms_any", "seniority_min", "employer_any", "metro_any", "is_founder"}
)


def _validate_spec(spec: dict) -> None:
    """Fail before touching the DB on a rule key `is_relevant` cannot honour.

    A typo'd key used to be silently ignored, which quietly WIDENED the ground
    truth (drop a constraint, everything looks relevant) — the same class of
    invisible pass this whole file is being rewritten to remove.
    """
    for case in spec["queries"]:
        rule = case.get("relevant") or {}
        unknown = set(rule) - _KNOWN_RULE_KEYS
        if unknown:
            raise ValueError(f"{case['id']}: unsupported rule keys {sorted(unknown)}")
        if rule.get("seniority_min") is not None:
            _rank_of(rule["seniority_min"])  # raises on an unknown rung


async def amain(args: argparse.Namespace) -> int:
    spec = yaml.safe_load((HERE / "queries.yaml").read_text(encoding="utf-8"))
    _validate_spec(spec)
    pool = await get_pool()
    unknown_facets: set[str] = set()
    queries_with_predicates = 0
    async with pool.acquire() as conn:
        corpus = await collect_labels(conn)
        if not corpus:
            print("no synth_ candidates; run scripts/seed_synthetic.py && foray derive")
            await close_pool()
            return 2
        print(f"corpus {len(corpus)} synth logins | arms={parse_arms()}")
        rows = []
        for case in spec["queries"]:
            relevant = {login for login, doc in corpus.items() if is_relevant(doc, case["relevant"])}
            if not relevant:
                # Not a ranking result — a broken fixture. nDCG 0.000 on an
                # empty ground truth reads exactly like a regression, and a
                # facet query whose column derive left NULL (an unmapped metro,
                # a title the seniority table missed) lands here first.
                print(
                    f"  WARN {case['id']}: 0 relevant rows - the ground-truth rule matched "
                    f"nothing, so every metric below is meaningless for it. Check that the "
                    f"facet columns it reads are populated."
                )
            result = await run_search(
                conn, case["query"], 40, use_llm=False, embed=hashing_embed
            )
            ranked = [hit["login"] for hit in result.results]
            labels = [login in relevant for login in ranked]
            # Junk labeling is by login marker, not corpus membership: an
            # unknown login is merely unlabeled, never counted as junk.
            labels_junk = [is_distractor(login) for login in ranked]
            diag = result.diagnostics or {}

            # Violations are judged against the predicates that ACTUALLY ran.
            summary = predicate_summary(diag)
            queries_with_predicates += bool(summary)
            expected = set(case.get("expect_facets") or [])
            missing = expected - {_FACET_ALIASES.get(k, k) for k in summary}
            if missing:
                print(
                    f"  WARN {case['id']}: expected facets {sorted(missing)} never became "
                    f"SQL - fv@10 cannot fail for them, and the funnel did not narrow on them"
                )
            labels_violation: list[bool] = []
            detail: list[str] = []
            for login in ranked:
                doc = corpus.get(login)
                if doc is None:
                    # Unlabeled (a non-synthetic row leaked into the corpus).
                    # Recorded as clean, NOT skipped: dropping it would shift
                    # every later rank into the @10 window and silently change
                    # what the metric measured.
                    labels_violation.append(False)
                    continue
                bad = facet_violations(doc, summary, unknown_facets)
                labels_violation.append(bool(bad))
                if bad and len(detail) < 3:
                    detail.append(f"{login}:{'+'.join(bad)}")
            rows.append(
                {
                    "id": case["id"],
                    "query": case["query"],
                    "relevant": len(relevant),
                    "returned": result.returned,
                    "p10": round(precision_at_k(labels, 10), 3),
                    "r10": round(recall_at_k(labels, len(relevant), 10), 3),
                    "mrr": round(mrr(labels), 3),
                    "ndcg10": round(ndcg_at_k(labels, len(relevant), 10), 3),
                    "junk10": round(junk_at_k(labels_junk, 10), 3),
                    "fv10": round(filter_violations_at_k(labels_violation, 10), 3),
                    "violation_detail": detail,
                    "predicates": sorted(summary),
                    # Funnel width: the headline number of the whole inversion.
                    # The claim is that a faceted query narrows 204k rows to
                    # hundreds; until now nothing printed it, so nobody could
                    # tell a facet that narrowed from a facet that parsed and
                    # did nothing. Reported, never asserted — the right value is
                    # query-dependent and a threshold here would be a guess.
                    "total_matched": result.total_matched,
                    "vector_hits": diag.get("vector_hits"),
                    "lexical_hits": diag.get("lexical_hits"),
                    "overlap": diag.get("overlap"),
                    "rrf_only_from_lexical@10": diag.get("rrf_only_from_lexical_at_10"),
                    "unscored_in_top_10": diag.get("unscored_in_top_10"),
                    "empty_lexical_query": diag.get("empty_lexical_query"),
                }
            )
            last = rows[-1]
            print(
                f"  {case['id']:22s} nDCG@10={last['ndcg10']:.3f}  "
                f"junk@10={last['junk10']:.3f}  fv@10={last['fv10']:.3f}  "
                f"matched={last['total_matched']}  "
                f"P@10={last['p10']:.3f}  unscored@10={last['unscored_in_top_10']}  "
                f"lex={last['lexical_hits']} vec={last['vector_hits']} "
                f"overlap={last['overlap']} lex-only@10={last['rrf_only_from_lexical@10']}"
            )
            if last["fv10"]:
                print(f"       violations: {', '.join(detail)}")
    await close_pool()
    mean = sum(r["ndcg10"] for r in rows) / len(rows) if rows else 0.0
    mean_junk = sum(r["junk10"] for r in rows) / len(rows) if rows else 0.0
    mean_fv = sum(r["fv10"] for r in rows) / len(rows) if rows else 0.0
    mean_matched = sum(r["total_matched"] for r in rows) / len(rows) if rows else 0.0
    print(
        f"mean nDCG@10 {mean:.3f} | mean junk@10 {mean_junk:.3f} | "
        f"mean fv@10 {mean_fv:.3f} | mean total_matched {mean_matched:.0f}"
    )
    # A metric that cannot fail is worse than one that fails: with no predicate
    # summary, fv@10 is 0.000 for every query and --max-violations passes while
    # proving nothing. Say so out loud rather than banking the green.
    if rows and not queries_with_predicates:
        print(
            "WARN no query produced a predicate summary - fv@10 is VACUOUS. Either "
            "no facet parsed, or diagnostics no longer carries `predicate_summary` "
            "(see predicate_summary() for the key names tried)."
        )
    if unknown_facets:
        print(
            f"WARN unrecognised facets in the predicate summary: {sorted(unknown_facets)} - "
            f"they ran as SQL and NOTHING checked them. Add a checker to _FACET_CHECKS."
        )
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "rows": rows,
                    "mean_ndcg10": mean,
                    "mean_junk10": mean_junk,
                    "mean_fv10": mean_fv,
                    "mean_total_matched": mean_matched,
                    "unknown_facets": sorted(unknown_facets),
                },
                indent=2,
            )
        )
    # Gate on junk, not just report it — the incident query LOOKED fine on nDCG
    # because the junk that outranked good hits was never labeled at all.
    if args.max_junk is not None and mean_junk > args.max_junk:
        print(f"FAIL mean junk@10 {mean_junk:.3f} > --max-junk {args.max_junk}")
        return 1
    # Zero is the DEFAULT, unlike --max-junk: junk is a ranking preference and
    # tolerating some of it is a judgement call, whereas a returned row that
    # violates a filter the system itself compiled to SQL is a bug with no
    # acceptable rate. Raise the flag only to characterise a known leak.
    if mean_fv > args.max_violations:
        print(f"FAIL mean fv@10 {mean_fv:.3f} > --max-violations {args.max_violations}")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", help="write a JSON report")
    ap.add_argument(
        "--max-junk",
        type=float,
        help="exit 1 if mean junk@10 (fraction of top-10 that are seeded distractors) exceeds this",
    )
    ap.add_argument(
        "--max-violations",
        type=float,
        default=0.0,
        help="exit 1 if mean fv@10 (fraction of top-10 breaking a filter that actually "
        "ran as SQL) exceeds this; defaults to 0 because a leaked hard filter is a bug",
    )
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
