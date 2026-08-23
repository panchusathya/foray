"""Closed query vocabularies: languages, metros, employers, and their hash.

Three tables live here, for one reason: a value that reaches a SQL predicate in
a spelling the column never stores does not error, it silently matches nothing.
Every entry below is a spelling-collapse rule that keeps the query side and the
derive side speaking the same language.

  * languages -- the two-spelling problem, documented at CANONICAL_LANGUAGES.
  * metros -- free-text location in, one canonical key out (``canonical_metro``,
    called by enrich/derive.py when it writes ``location_metro`` and by the
    parser when it reads a query, so the two cannot drift).
  * employers -- ``canonical_employer`` reuses enrich/employer.py's alias table
    rather than starting a second one.

``vocab_hash()`` fingerprints all of it plus the archetype id set, and is
folded into the query-parse cache key: editing an alias file must invalidate
cached parses, or a recruiter keeps getting yesterday's vocabulary.

The same language legitimately has TWO spellings in this system:

  * the STORED form — the lowercased GitHub GraphQL language name that
    enrich/derive.py writes into ``primary_language`` and the language mix
    ("c++", "c#"), and
  * the GitHub SEARCH-QUALIFIER form that harvest shard queries need
    ("cpp", "csharp" — ``language:c++`` is not a valid search qualifier).

Before this module existed, harvest/shards.py owned a single LANGUAGES list in
qualifier spelling and api/parse.py matched queries against it, so a recruiter
typing "c++" produced ``primary_language = any('{cpp}')`` — which matches ZERO
rows, ever, because derive stores "c++". This module keeps both spellings,
derived from one canonical list, so they cannot drift apart again.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache

import yaml

from .config import settings

# The stored forms (what derive writes, what SQL filters must use). Order is
# load-bearing twice over: the first 12 entries mirror harvest/shards.py's
# historical list order so the derived shard grid is byte-identical to the
# pre-vocab one, and match_languages ties break on it.  "c#" and "c" extend
# coverage for query matching only (see _NOT_HARVESTED below).
CANONICAL_LANGUAGES: tuple[str, ...] = (
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
    "c++",
    "c#",
    "c",
)

# Aliases normalize what recruiters TYPE; the canonical list is what derive
# WRITES. Every value must be a member of CANONICAL_LANGUAGES.
LANGUAGE_ALIASES: dict[str, str] = {
    "cpp": "c++",
    "cplusplus": "c++",
    "c plus plus": "c++",
    "golang": "go",
    "ts": "typescript",
    "js": "javascript",
    "csharp": "c#",
    "c sharp": "c#",
    "py": "python",
}

# stored form -> GitHub search-qualifier spelling, only where they differ.
_STORED_TO_GITHUB_QUALIFIER: dict[str, str] = {
    "c++": "cpp",
    "c#": "csharp",
}

# Languages we can match in queries but deliberately do NOT shard the GitHub
# harvest on: the 40x12x6x11 grid was sized and validated at exactly these 12
# languages (test_harvest pins the count), and ``language:c`` in particular
# drags in a sea of forked/vendored embedded code. Growing the grid is a
# harvest capacity decision, not a vocabulary side effect.
_NOT_HARVESTED: frozenset[str] = frozenset({"c#", "c"})

GITHUB_SEARCH_LANGUAGES: tuple[str, ...] = tuple(
    _STORED_TO_GITHUB_QUALIFIER.get(lang, lang)
    for lang in CANONICAL_LANGUAGES
    if lang not in _NOT_HARVESTED
)


def _token(word: str) -> re.Pattern[str]:
    # Hand-rolled token boundaries instead of \b: \b is defined relative to
    # \w, which breaks for names containing non-word characters — \b after
    # '+' demands a FOLLOWING word character, so rf"\b{re.escape('c++')}\b"
    # can never match "c++" at end-of-token. That dead regex is exactly why
    # heuristic_parse never extracted C++ from a query. Plain-word languages
    # get lookaround boundaries that mean "not glued to another [a-z0-9]".
    return re.compile(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])")


# stored form -> compiled pattern matching that language as a token in
# LOWERCASED free text. Alias forms ("cpp", "golang", ...) are matched via
# LANGUAGE_ALIASES at match time rather than baked into these patterns, so
# adding an alias never requires touching a regex.
LANGUAGE_PATTERNS: dict[str, re.Pattern[str]] = {
    lang: _token(lang) for lang in CANONICAL_LANGUAGES if lang not in ("c++", "c#", "c")
}
# The punctuation-named C family needs hand-written boundaries:
#   * "c++"/"c#": the left edge must also reject '+' so "c+++" / "vc++" style
#     runs don't double-count; the right edge rejects more of the same symbol.
#   * bare "c": must NOT fire inside "c++", "c#", or the spelled-out aliases
#     "c sharp" / "c plus plus" (those resolve to c#/c++ via the alias table,
#     and emitting a bogus bare "c" alongside would poison the SQL filter).
#     "objective-c" still emits "c" — accepted; the corpus stores no
#     objective-c and a hyphen-preceded c is otherwise always the language.
LANGUAGE_PATTERNS["c++"] = re.compile(r"(?<![a-z0-9+])c\+\+(?![+#])")
LANGUAGE_PATTERNS["c#"] = re.compile(r"(?<![a-z0-9+#])c#(?![a-z0-9#])")
LANGUAGE_PATTERNS["c"] = re.compile(
    r"(?<![a-z0-9+#])c(?![a-z0-9+#])(?!\s+(?:sharp|plus\s+plus)(?![a-z0-9]))"
)

# Aliases are matched with the same token boundaries as plain-word languages;
# compiled once here because match_languages sits on the per-query hot path.
_ALIAS_PATTERNS: dict[str, tuple[str, re.Pattern[str]]] = {
    alias: (lang, _token(alias)) for alias, lang in LANGUAGE_ALIASES.items()
}


def match_languages(text: str) -> list[str]:
    """Stored-form languages mentioned in free text, in order of appearance.

    Appearance order (not canonical order) so "c# and typescript" returns
    ["c#", "typescript"] — the shape a recruiter reading the parse echo
    expects. Deduped: a query hitting both an alias and the canonical form
    ("cpp / c++ developer") yields the stored form once, at its earliest hit.
    """
    q = text.lower()
    earliest: dict[str, int] = {}
    for lang, pattern in LANGUAGE_PATTERNS.items():
        found = pattern.search(q)
        if found is not None and (lang not in earliest or found.start() < earliest[lang]):
            earliest[lang] = found.start()
    for lang, pattern in _ALIAS_PATTERNS.values():
        found = pattern.search(q)
        if found is not None and (lang not in earliest or found.start() < earliest[lang]):
            earliest[lang] = found.start()
    return sorted(earliest, key=earliest.__getitem__)


# --------------------------------------------------------------------- metros --

# Tokens that qualify a place rather than name one. Stripped before the
# exact-match test for two-character aliases only (see canonical_metro): the
# containment path does not need them, and stripping them globally would let
# "Washington" swallow "Washington DC".
_REGION_TOKENS: frozenset[str] = frozenset(
    {
        "area",
        "bay",  # only meaningful attached to a city name, which is its own alias
        "ca",
        "county",
        "greater",
        "metro",
        "region",
        "us",
        "usa",
        "united",
        "states",
        "ny",
        "wa",
        "tx",
        "ma",
        "co",
        "il",
        "ga",
        "fl",
        "pa",
        "mi",
        "wi",
        "ut",
        "az",
        "nc",
        "tn",
        "mn",
        "va",
        "md",
    }
)

# A two-token alias must survive punctuation between the tokens ("San-Jose",
# "Washington, D.C."), so aliases are compiled with a separator class rather
# than matched as literals.
_SEP = r"[^a-z0-9]+"

# Aliases at or below this length are NOT matched by containment on a stored
# location field. "sf" inside "Surfside" and "la" inside "La Jolla" would both
# mis-file a row, and location_metro is written once for 204k rows -- a false
# positive there is a permanently wrong facet value. In a QUERY the same alias
# is worth matching (a recruiter typing "in SF" means the Bay Area, and the
# blast radius of a rare false positive is one search), which is why
# metro_spans and canonical_metro treat them differently.
_SHORT_ALIAS_CHARS = 2

# pg_trgm cannot use a GIN trigram index for a LIKE pattern with fewer than 3
# characters of literal text -- it has no trigram to look up, so the plan
# degenerates to the seq scan this whole facet redesign exists to remove
# (measured: 219 ms / 38k buffers for ONE metro predicate, x4 statements per
# request). Short aliases therefore never become LIKE patterns; they resolve to
# a canonical key, and the key is matched by equality on location_metro.
_MIN_TRIGRAM_CHARS = 3


def _norm(text: str) -> str:
    """Lowercase, punctuation-to-space, whitespace-collapsed."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


@lru_cache(maxsize=1)
def metro_alias_table() -> dict[str, tuple[str, ...]]:
    """canonical key -> normalized aliases, as loaded from metro_aliases.yaml."""
    path = settings.data_dir / "metro_aliases.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    table: dict[str, tuple[str, ...]] = {}
    for key, aliases in raw.items():
        cleaned = tuple(
            dict.fromkeys(filter(None, (_norm(str(a)) for a in (aliases or []))))
        )
        if cleaned:
            table[str(key).strip().lower()] = cleaned
    return table


def metro_keys() -> tuple[str, ...]:
    """The closed key set, sorted. Advertised verbatim in the LLM parse prompt:
    a model that cannot see the key set emits "SF Bay Area" and gets dropped."""
    return tuple(sorted(metro_alias_table()))


@lru_cache(maxsize=1)
def _alias_patterns() -> tuple[tuple[str, str, re.Pattern[str]], ...]:
    """(alias, key, compiled pattern), LONGEST ALIAS FIRST.

    Longest-first is load-bearing: "la jolla" (San Diego) must win over the
    bare "la" (Los Angeles), and "washington dc" over any future "washington".
    """
    entries: list[tuple[str, str, re.Pattern[str]]] = []
    for key, aliases in metro_alias_table().items():
        for alias in aliases:
            body = _SEP.join(re.escape(tok) for tok in alias.split(" "))
            entries.append(
                (alias, key, re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])"))
            )
    entries.sort(key=lambda item: (-len(item[0]), item[0]))
    return tuple(entries)


def canonical_metro(raw: str | None) -> str | None:
    """Free-text location -> canonical metro key, or None when unmappable.

    This is the signature enrich/derive.py calls to write ``location_metro``,
    and the one the parser calls on a query's location span, so a stored value
    and a searched value can never disagree.

    Idempotent on its own output: a canonical key maps to itself, which is what
    lets ``coerce`` re-validate an already-parsed filter (and a re-derive
    re-normalize an already-normalized column) without eating its own tail.

    None is a real answer, not a failure. "Bengaluru" has no key, and the
    caller's job is to DROP the facet and log it -- a metro predicate that
    matches nothing is worse than no metro predicate, because the recruiter
    reads zero results as "nobody like this exists".
    """
    if raw is None:
        return None
    text = raw.strip().lower()
    if not text:
        return None
    if text in metro_alias_table():
        return text
    for alias, key, pattern in _alias_patterns():
        if len(alias) > _SHORT_ALIAS_CHARS and pattern.search(text):
            return key
    # Short aliases, exact match only, after dropping state/country/qualifier
    # tokens: "SF", "SF, CA" and "sf ca usa" are the Bay Area; "La Jolla" and
    # "Surfside" are not LA and not the Bay Area. Containment here would
    # mis-file rows permanently (see _SHORT_ALIAS_CHARS).
    stripped = " ".join(
        tok for tok in _norm(text).split(" ") if tok not in _REGION_TOKENS
    )
    for alias, key, _pattern in _alias_patterns():
        if len(alias) <= _SHORT_ALIAS_CHARS and stripped == alias:
            return key
    return None


def metro_spans(text: str) -> list[tuple[int, int, str]]:
    """Non-overlapping (start, end, key) metro mentions in LOWERCASED text.

    Query-side counterpart to canonical_metro, and deliberately LOOSER: short
    aliases ("in SF", "hiring in LA") match by containment here. The spans are
    returned, not just the keys, because heuristic_parse must REMOVE what it
    consumed from free_text -- a facet word that stays in free_text reaches the
    lexical tsquery, which is how "senior" came to rank Staff Accountants.
    """
    if not text:
        return []
    low = text.lower()
    spans: list[tuple[int, int, str]] = []
    taken: list[tuple[int, int]] = []
    for _alias, key, pattern in _alias_patterns():
        for found in pattern.finditer(low):
            start, end = found.span()
            if any(start < t_end and t_start < end for t_start, t_end in taken):
                continue  # longest alias already claimed this text
            taken.append((start, end))
            spans.append((start, end, key))
    spans.sort()
    return spans


@lru_cache(maxsize=64)
def metro_like_patterns(key: str) -> tuple[str, ...]:
    """Aliases usable as trigram LIKE bodies for one key, minimal set.

    Two prunings, both about the index rather than about correctness:

      * aliases shorter than 3 chars are dropped -- see _MIN_TRIGRAM_CHARS.
      * an alias CONTAINING another kept alias is dropped, because
        ``%san francisco%`` already matches every row ``%san francisco bay
        area%`` would. metros.txt lists both as peers (it is a shard grid);
        emitting both here would just be a second index scan for no rows.
    """
    aliases = sorted(
        (a for a in metro_alias_table().get(key, ()) if len(a) >= _MIN_TRIGRAM_CHARS),
        key=lambda a: (len(a), a),
    )
    kept: list[str] = []
    for alias in aliases:
        if not any(shorter in alias for shorter in kept):
            kept.append(alias)
    return tuple(kept)


# ------------------------------------------------------------------ employers --


def canonical_employer(raw: str | None) -> str | None:
    """Employer string -> the canonical name enrich/employer.py would store,
    lowercased for comparison against ``lower(s.employer)``. None when the
    alias table does not know the company.

    None means "no fast path", not "invalid": employers are an OPEN vocabulary,
    so the search layer keeps the value and matches it with a trigram LIKE
    instead of an equality. The distinction is what makes the equality safe --
    ``lower(s.employer) = 'google'`` is only worth emitting because
    infer_employer maps "@google", "googlers" and "alphabet" to exactly
    "Google" before storing.

    Measured need, two ways:
      * "University of California, San Francisco" and "UCSF" are separate
        employers in the corpus today; neither is in company_aliases.yaml, so
        both fall to the trigram arm rather than pretending to be one key.
      * employer='LinkedIn' was the #1 employer at 6,180 rows before Stage 1
        killed the Bright Data phantoms (~88% fake) -- an employer facet built
        on unnormalized strings inherits that kind of poison silently.

    Known wave-1 gap, deliberately not papered over here: derive canonicalizes
    the GITHUB employer (infer_employer -> _canonical_company) but stores the
    LinkedIn ``company`` field verbatim, so "Google LLC" exists alongside
    "Google" and only the trigram arm reaches it. The fix belongs in derive,
    not in a second normalization table on the query side.
    """
    if raw is None:
        return None
    # Imported inside the function: enrich/employer.py owns the alias table and
    # its lru_cached loader, and vocab is imported by harvest/derive/parse --
    # keeping the edge lazy means none of them pay for a YAML read they do not
    # use, and a future enrich -> vocab import cannot become a cycle.
    from .enrich.employer import _aliases, _canonical_company

    cleaned = raw.strip().lstrip("@").strip()
    if not cleaned:
        return None
    if cleaned.lower() not in _aliases():
        return None
    return _canonical_company(cleaned).lower()


# ----------------------------------------------------------------- vocab hash --


@lru_cache(maxsize=1)
def vocab_hash() -> str:
    """Fingerprint of every closed vocabulary the parser depends on.

    Folded into parse.py's cache key, so editing metro_aliases.yaml (or
    company_aliases.yaml, or archetypes.yaml's id set) makes every cached parse
    unreachable instead of serving yesterday's vocabulary forever. Cheap:
    ``query_parse_cache`` rows are regenerable by definition.

    Covers the TABLES, not the compiled patterns -- the patterns are derived
    from the tables, so hashing both would only add a way for the two to
    disagree.
    """
    from .enrich.employer import _aliases
    from .score.archetypes import load_archetypes

    digest = hashlib.sha256()

    def feed(*parts: str) -> None:
        # \x00 separators: without them, ("ab","c") and ("a","bc") hash alike,
        # which is exactly how an alias split lands in the wrong bucket.
        digest.update("\x00".join(parts).encode("utf-8"))
        digest.update(b"\x1e")

    feed("languages", *CANONICAL_LANGUAGES)
    for alias in sorted(LANGUAGE_ALIASES):
        feed("language_alias", alias, LANGUAGE_ALIASES[alias])
    for key, aliases in sorted(metro_alias_table().items()):
        feed("metro", key, *aliases)
    for raw, canonical in sorted(_aliases().items()):
        feed("employer", raw, str(canonical))
    feed("archetypes", *sorted(load_archetypes()))
    return digest.hexdigest()
