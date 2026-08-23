"""Single source of truth for programming-language naming.

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

import re

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
