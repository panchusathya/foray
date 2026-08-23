"""The only door into ParsedFilter. Both parsers exit through it.

Before this module, ``llm_parse`` built a ParsedFilter straight out of
``json.loads`` output and ``_filter_clauses`` turned it into SQL: whatever the
model said became a predicate. With six fields that was survivable. With the
wave-1 facet set -- titles and employers matched by LIKE, metros and languages
matched against columns whose spelling is fixed by derive -- it is not.

Three hazards, each closed below and each named in the plan:

  a. UNMAPPABLE VALUES. A closed-vocabulary value the corpus never stores
     ("SF Bay Area" where location_metro holds "sf_bay_area", "Bengaluru" where
     no key exists) produces a predicate that matches nothing, and zero results
     reads to a recruiter as "nobody like this exists" rather than "we do not
     support that filter". Such values are DROPPED and logged.
  b. UNBOUNDED WORK. List length and item length are the model's to choose,
     and every text facet becomes an OR-chain of index scans. Caps here bound
     the width of the SQL the planner sees.
  c. WILDCARD INJECTION. ``%`` / ``_`` / ``\\`` reaching a LIKE pattern.

None of this is SQL injection defence -- every value below is passed to asyncpg
as a bind parameter and cannot terminate a string literal. It is defence
against a self-inflicted denial of service and against silent wrongness.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..enrich.markers import load_markers
from ..log import get_logger
from ..score.archetypes import load_archetypes
from ..vocab import (
    CANONICAL_LANGUAGES,
    LANGUAGE_ALIASES,
    canonical_metro,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle, see coerce() below
    from .parse import ParsedFilter

log = get_logger("api.coerce")

# Hazard (b). Both numbers are about the SQL, not about the JSON: each text
# facet value becomes one ``lower(col) like $n`` arm, so a 200-item titles list
# is a 200-way OR-chain rebuilt for the count + vector + lexical + enrich
# statements. Eight is well past what any real brief states, and a brief that
# needs more is asking for "anyone", which is what no filter already means.
MAX_LIST_ITEMS = 8
# 64 chars covers "senior staff machine learning infrastructure engineer" with
# room to spare. Longer strings are model babble or a pasted job description,
# and a 2 KB LIKE body is pure planner cost for a pattern nothing can match.
MAX_ITEM_CHARS = 64
# free_text is not a predicate -- it is the embedding + tsquery input -- but it
# is still unbounded model output on the request path. build_websearch_or caps
# itself at 12 terms; this caps what the local embedder has to tokenize.
MAX_FREE_TEXT_CHARS = 1000

# Hazard (c). LIKE metacharacters, stripped rather than escaped.
#
# Escaping would be defensible for a user typing a literal "%" into a job
# title, which does not happen; stripping is one rule with no escape-clause
# interaction. Why it matters concretely: a model-emitted ``employers: ["%"]``
# becomes ``lower(s.employer) like '%%%'`` -- every non-null employer matches,
# the trigram GIN index has no trigram to look up, and the facet silently
# becomes a full scan of 204,535 rows on FOUR statements per request. A bare
# ``_`` is subtler: it matches any single character, so the predicate is simply
# a different question than the one asked, with no error either way. A trailing
# backslash is worse than either -- it is LIKE's escape character, so the
# pattern is malformed and asyncpg raises 22025 and 500s the whole search.
#
# To say it precisely: these are bind parameters, so this is NOT SQL
# injection. It is a self-inflicted denial of service (and a silent
# mis-predicate) that the query planner cannot warn us about.
_LIKE_METACHARS = re.compile(r"[%_\\]")

# Considered and NOT done: a minimum length for LIKE bodies. pg_trgm can
# extract no trigrams from a two-character pattern, so `%ml%` degenerates to a
# full INDEX scan (not a heap seq scan — a real but bounded cost). Dropping
# short values would also throw away legitimate ones: "ml" and "ai" are exactly
# how a title substring gets written. Revisit with a measurement, not a guess.

_SOURCES = frozenset({"github", "linkedin", "both"})

# Hazard (b), numeric arm. Clamped rather than dropped: a model that says
# "experience_min: 200" meant "very senior", and clamping keeps a usable
# filter, while a rejected bound would silently widen the search.
_EXPERIENCE_BOUNDS = (0.0, 50.0)
# The IC ladder is a smallint 1..6 (see parse.SENIORITY_WORDS). Out-of-range
# values are the model inventing a scale.
_SENIORITY_BOUNDS = (1, 6)
# 600 months = 50 years in one role. tenure_months is an int column.
_TENURE_BOUNDS = (0, 600)


def _drop(facet: str, value: Any) -> None:
    """Log one dropped facet value.

    This log is the FACET ROADMAP. Every line is the model (i.e. a recruiter's
    phrasing) asking for something the corpus cannot answer -- a metro with no
    key, a marker file that does not exist, a source name we do not model. The
    aggregate over a week is a ranked list of which facet to build next, which
    is information no amount of design discussion produces.
    """
    log.info("parse.facet_dropped", facet=facet, value=str(value)[:120])


def _as_list(value: Any) -> list[str]:
    """Model output is not trusted to be a list of strings.

    A bare string is accepted as a one-item list (models emit ``"titles":
    "ML engineer"`` routinely); anything else non-scalar is dropped by the
    per-item guard below.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None and not isinstance(v, (dict, list))]
    return []


def _clean_items(facet: str, values: Any, *, strip_wildcards: bool) -> list[str]:
    """Trim, cap, dedupe, and (for LIKE facets) strip metacharacters."""
    out: list[str] = []
    for raw in _as_list(values):
        item = " ".join(str(raw).split())[:MAX_ITEM_CHARS].strip()
        if strip_wildcards:
            item = _LIKE_METACHARS.sub("", item).strip()
        if not item:
            # An item that was ONLY wildcards is the hazard-(c) case; it must
            # not survive as an empty pattern (which matches nothing) either.
            _drop(facet, raw)
            continue
        if item.lower() in {o.lower() for o in out}:
            continue
        out.append(item)
        if len(out) >= MAX_LIST_ITEMS:
            break
    return out


def _closed(facet: str, values: Any, resolve) -> list[str]:
    """Map each value through a closed vocabulary, dropping what will not map."""
    out: list[str] = []
    for item in _clean_items(facet, values, strip_wildcards=False):
        mapped = resolve(item)
        if mapped is None:
            _drop(facet, item)
            continue
        if mapped not in out:
            out.append(mapped)
    return out


def _language(raw: str) -> str | None:
    key = " ".join(raw.split()).lower()
    key = LANGUAGE_ALIASES.get(key, key)
    return key if key in CANONICAL_LANGUAGES else None


def _marker(raw: str) -> str | None:
    key = raw.strip().lower()
    return key if key in load_markers() else None


def _archetype(raw: str) -> str | None:
    key = raw.strip()
    return key if key in load_archetypes() else None


def _topic(raw: str) -> str | None:
    # Topics are an OPEN vocabulary (they are GitHub repo topics), so there is
    # nothing to validate against -- but they are matched with array overlap,
    # not LIKE, so the comparison is exact and case-sensitive. GitHub
    # normalizes topic names to lowercase at the source and derive stores them
    # as given, so lowercasing here is what makes ``s.topics && $n`` able to
    # hit at all.
    key = raw.strip().lower()
    return key or None


def _number(facet: str, value: Any, bounds: tuple[float, float]) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        _drop(facet, value)
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        _drop(facet, value)
        return None
    low, high = bounds
    return min(max(number, low), high)


def _int(facet: str, value: Any, bounds: tuple[int, int]) -> int | None:
    number = _number(facet, value, (float(bounds[0]), float(bounds[1])))
    return None if number is None else round(number)


def _flag(value: Any) -> bool | None:
    """Tri-state facets stay tri-state: None means "not asked", which is not
    the same predicate as False (``is_manager=False`` emits ``mgmt_rank is
    null``)."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "1"}:
            return True
        if text in {"false", "no", "0"}:
            return False
        return None
    return bool(value)


def _bool(value: Any) -> bool:
    return bool(_flag(value))


def coerce(raw: dict, *, query: str = "") -> ParsedFilter:
    """Validate one raw filter dict into a ParsedFilter.

    Called at the END of both heuristic_parse and llm_parse, so there is one
    place where a value becomes eligible to reach SQL. The heuristic parser's
    output is already canonical, and passing it through anyway is deliberate:
    every canonicalizer here is idempotent, and a second door would be a second
    place for the rules to drift.
    """
    # Imported here, not at module scope: parse.py owns ParsedFilter and calls
    # coerce(), so a module-level ``from .parse import ParsedFilter`` is a hard
    # import cycle (importing foray.api.parse first fails on the half-built
    # module). TYPE_CHECKING covers the annotation.
    from .parse import ParsedFilter

    free_text = str(raw.get("free_text") or query or "").strip()[:MAX_FREE_TEXT_CHARS]
    return ParsedFilter(
        archetype_id=(
            _archetype(str(raw["archetype_id"]))
            if raw.get("archetype_id") is not None
            else None
        ),
        metros=_closed("metros", raw.get("metros"), canonical_metro),
        languages=_closed("languages", raw.get("languages"), _language),
        markers=_closed("markers", raw.get("markers"), _marker),
        topics=_closed("topics", raw.get("topics"), _topic),
        # Open vocabularies: nothing to validate against, so the wildcard strip
        # is the ONLY guard between model output and a LIKE pattern.
        titles=_clean_items("titles", raw.get("titles"), strip_wildcards=True),
        title_exclude=_clean_items(
            "title_exclude", raw.get("title_exclude"), strip_wildcards=True
        ),
        employers=_clean_items("employers", raw.get("employers"), strip_wildcards=True),
        experience_min=_number("experience_min", raw.get("experience_min"), _EXPERIENCE_BOUNDS),
        experience_max=_number("experience_max", raw.get("experience_max"), _EXPERIENCE_BOUNDS),
        tenure_min_months=_int(
            "tenure_min_months", raw.get("tenure_min_months"), _TENURE_BOUNDS
        ),
        tenure_max_months=_int(
            "tenure_max_months", raw.get("tenure_max_months"), _TENURE_BOUNDS
        ),
        seniority_min=_int("seniority_min", raw.get("seniority_min"), _SENIORITY_BOUNDS),
        seniority_max=_int("seniority_max", raw.get("seniority_max"), _SENIORITY_BOUNDS),
        is_manager=_flag(raw.get("is_manager")),
        is_founder=_flag(raw.get("is_founder")),
        source=_source(raw.get("source")),
        require_code_evidence=_bool(raw.get("require_code_evidence")),
        free_text=free_text,
        # Policy switches are NOT model-settable. No natural-language phrase
        # maps to "include unknown seniority", and a model that guessed one
        # would be silently choosing which source population to delete. They
        # are honoured here only when a CALLER (tests, the eval harness, a
        # future UI toggle) passes them in the dict explicitly.
        strict_language=_bool(raw.get("strict_language")),
        include_unknown_experience=_bool(raw.get("include_unknown_experience")),
        include_unknown_seniority=(
            True
            if raw.get("include_unknown_seniority") is None
            else _bool(raw.get("include_unknown_seniority"))
        ),
        include_unknown_tenure=_bool(raw.get("include_unknown_tenure")),
        strict_topics=_bool(raw.get("strict_topics")),
        strict_titles=_bool(raw.get("strict_titles")),
    )


def _source(value: Any) -> str | None:
    if value is None:
        return None
    key = str(value).strip().lower()
    if key in _SOURCES:
        return key
    _drop("source", value)
    return None
