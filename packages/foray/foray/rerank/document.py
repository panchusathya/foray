"""Purpose-built cross-encoder documents. Not the scoring prompt, not embed_blob.

Front-load languages / seniority / employer, then a short bio sentence, then
repos with stars, topics, and commit volume. LinkedIn-only rows drop every
repo/star/commit line: "0 stars, 0 commits" reads as a bad engineer to any
relevance model. Query side is the raw free text; bge's QUERY_INSTRUCTION is
a bge-asymmetric artifact and pure noise to an ms-marco model.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def _as_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def build_document(row: Mapping[str, Any], *, max_chars: int = 900) -> str:
    kind = row.get("evidence_kind") or "github"
    parts: list[str] = []

    language = row.get("primary_language") or ""
    years = row.get("est_experience_years")
    employer = row.get("employer") or ""
    title = row.get("title") or ""
    header_bits = [bit for bit in (language, title, employer) if bit]
    if years is not None:
        header_bits.append(f"~{float(years):.0f}y")
    if header_bits:
        parts.append(" · ".join(header_bits))

    blob = (row.get("embed_blob") or "")[:300]
    if blob:
        # First line of the blob is name + bio; fine as CE input, never as a
        # displayed highlight (bios contain hedge words and self-description).
        first = blob.split("\n", 1)[0].strip()
        if first:
            parts.append(first)

    if kind != "linkedin":
        repos = _as_json(row.get("top_repos"), []) or []
        repo_bits = []
        for repo in repos[:4]:
            name = repo.get("name") or ""
            if not name:
                continue
            stars = int(repo.get("stars") or 0)
            repo_bits.append(f"{name} ({stars}★)")
        if repo_bits:
            parts.append("Repos: " + "; ".join(repo_bits))
        topics = list(row.get("topics") or [])[:8]
        if topics:
            parts.append("Topics: " + ", ".join(str(t) for t in topics))
        commits = int(row.get("commits_last_year") or 0)
        prs = int(row.get("prs_last_year") or 0)
        if commits or prs:
            parts.append(f"{commits} commits, {prs} PRs last year")
        markers = list(row.get("domain_markers") or [])
        if markers:
            parts.append("Markers: " + ", ".join(str(m) for m in markers[:8]))
    else:
        markers = list(row.get("domain_markers") or [])
        if markers:
            parts.append("Domains: " + ", ".join(str(m) for m in markers[:8]))

    text = ". ".join(parts)
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text
