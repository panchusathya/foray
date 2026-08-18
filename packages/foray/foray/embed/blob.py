"""Build the stable text blob that gets embedded.

Order matters for embedding quality: lead with the most distinctive content
(name/bio), then languages, employer, notable repos, and topics. The blob is
stored on ``candidates.embed_blob`` so an unchanged profile can skip re-embedding.
"""

from __future__ import annotations

MAX_BLOB_CHARS = 2000  # ~512 tokens for bge-small


def build_blob(
    *,
    name: str | None,
    login: str,
    bio: str | None,
    primary_language: str | None,
    language_mix: dict[str, float] | None,
    employer: str | None,
    title: str | None,
    top_repos: list[dict] | None,
    topics: list[str] | None,
    max_chars: int = MAX_BLOB_CHARS,
) -> str:
    lines: list[str] = []

    header = (name or login or "").strip()
    if bio:
        header = f"{header}. {bio.strip()}" if header else bio.strip()
    if header:
        lines.append(header)

    if primary_language:
        others = [lang for lang in (language_mix or {}) if lang != primary_language][:4]
        line = f"Primary: {primary_language}."
        if others:
            line += " Also: " + ", ".join(others) + "."
        lines.append(line)

    if employer:
        lines.append(f"{title} at {employer}" if title else employer)

    if top_repos:
        parts = []
        for repo in top_repos[:5]:
            desc = (repo.get("description") or "").strip()
            lang = repo.get("language") or "n/a"
            parts.append(f"{repo.get('name')} — {desc} ({repo.get('stars', 0)} stars, {lang})")
        lines.append("Notable work: " + "; ".join(parts))

    if topics:
        lines.append("Topics: " + ", ".join(topics[:20]))

    return "\n".join(lines)[:max_chars]
