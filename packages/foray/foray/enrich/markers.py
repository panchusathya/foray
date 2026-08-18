"""Domain markers: cheap, LLM-free SQL-friendly tags that feed the Phase 4 gate.

A marker is awarded if any of its terms appear (as a whole token, so "go" does
not match "google") in a candidate's repo topics, descriptions, or names. For
``ml`` and ``frontend`` we additionally require the relevant language to exceed
0.2 of the language mix, or every repo containing a stray .py/.js file would
match.
"""

from __future__ import annotations

import re
from functools import lru_cache

from ..config import settings

# Markers that also require a language-mix threshold to fire.
LANGUAGE_GATE = {
    "ml": ({"python"}, 0.2),
    "frontend": ({"javascript", "typescript"}, 0.2),
}


@lru_cache(maxsize=1)
def load_markers() -> dict[str, list[str]]:
    markers: dict[str, list[str]] = {}
    marker_dir = settings.data_dir / "markers"
    for path in sorted(marker_dir.glob("*.txt")):
        terms = [
            line.strip().lower()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if terms:
            markers[path.stem] = terms
    return markers


@lru_cache(maxsize=2048)
def _term_pattern(term: str) -> re.Pattern[str]:
    # Whole-token match: no alnum on either side. Hyphens/dots inside terms are
    # matched literally (e.g. "pytest-plugin", "next.js").
    return re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])")


def _haystack(repos: list[dict]) -> str:
    parts: list[str] = []
    for repo in repos:
        if repo.get("name"):
            parts.append(str(repo["name"]).replace("_", "-"))
        if repo.get("description"):
            parts.append(str(repo["description"]))
        for topic in repo.get("topics", []):
            parts.append(str(topic))
    return " ".join(parts).lower()


def detect_markers(repos: list[dict], language_mix: dict[str, float]) -> list[str]:
    haystack = _haystack(repos)
    awarded: list[str] = []
    for marker, terms in load_markers().items():
        if not any(_term_pattern(term).search(haystack) for term in terms):
            continue
        if marker in LANGUAGE_GATE:
            langs, threshold = LANGUAGE_GATE[marker]
            frac = sum(language_mix.get(lang, 0.0) for lang in langs)
            if frac <= threshold:
                continue
        awarded.append(marker)
    return sorted(awarded)
