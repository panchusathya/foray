"""Domain markers: cheap, LLM-free SQL-friendly tags that feed the Phase 4 gate.

A marker is awarded if any of its terms appear (as a whole token, so "go" does
not match "google") in a candidate's repo topics, descriptions, or names, or in
the GitHub bio. For ``ml`` and ``frontend`` we additionally require the relevant
language to exceed 0.2 of the language mix, or every repo containing a stray
.py/.js file would match.

Strong ML terms (trained-model / systems vocabulary) bypass the language gate:
a CUDA/C++ engineer whose repos mention megatron or vllm is an ML engineer even
when Python is a minority of the mix. Weak terms (transformers, clip, rag) keep
the gate — they are too common in unrelated Python repos, and too ambiguous
without it.
"""

from __future__ import annotations

import re
from functools import lru_cache

from ..config import settings

# Markers that also require a language-mix threshold to fire, unless a strong
# term (see STRONG_ML_TERMS) matched for that marker.
LANGUAGE_GATE = {
    "ml": ({"python"}, 0.2),
    "frontend": ({"javascript", "typescript"}, 0.2),
}

# Bypass the Python-fraction gate. Keep this set tight: clip/rag/vit/whisper/
# diffusion/embeddings stay weak because they fire on non-ML corpus at scale.
STRONG_ML_TERMS = {
    "llm",
    "vlm",
    "multimodal",
    "vision-language",
    "rlhf",
    "dpo",
    "lora",
    "peft",
    "fsdp",
    "deepspeed",
    "megatron",
    "llama.cpp",
    "vllm",
    "sglang",
    "tensorrt",
    "triton",
    "mistral",
    "qwen",
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
    # matched literally (e.g. "pytest-plugin", "next.js"). A trailing ``s`` is
    # allowed so "VLMs" / "LLMs" in a bio still award the canonical term.
    if term.endswith("s"):
        return re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])")
    return re.compile(rf"(?<![a-z0-9]){re.escape(term)}s?(?![a-z0-9])")


def _haystack(repos: list[dict], bio: str | None = None) -> str:
    parts: list[str] = []
    for repo in repos:
        if repo.get("name"):
            parts.append(str(repo["name"]).replace("_", "-"))
        if repo.get("description"):
            parts.append(str(repo["description"]))
        for topic in repo.get("topics", []):
            parts.append(str(topic))
    if bio:
        parts.append(str(bio)[:2000])
    return " ".join(parts).lower()


def detect_markers(
    repos: list[dict],
    language_mix: dict[str, float],
    *,
    require_language_gate: bool = True,
    bio: str | None = None,
) -> list[str]:
    """``require_language_gate=False`` is for LinkedIn-derived pseudo-repos:
    the gate exists to stop a stray .py file awarding ``ml``, but a profile
    that *says* machine learning is a deliberate claim, and LinkedIn people
    have no language mix to clear the threshold with.

    ``bio`` is the GitHub bio (LinkedIn already folds about-text into
    pseudo-repos). It is the cheapest way to catch "I train VLMs" people whose
    repos are named ``train-loop``.
    """
    haystack = _haystack(repos, bio)
    awarded: list[str] = []
    for marker, terms in load_markers().items():
        matched = [term for term in terms if _term_pattern(term).search(haystack)]
        if not matched:
            continue
        if require_language_gate and marker in LANGUAGE_GATE:
            strong_hit = marker == "ml" and any(term in STRONG_ML_TERMS for term in matched)
            if not strong_hit:
                langs, threshold = LANGUAGE_GATE[marker]
                frac = sum(language_mix.get(lang, 0.0) for lang in langs)
                if frac <= threshold:
                    continue
        awarded.append(marker)
    return sorted(awarded)
