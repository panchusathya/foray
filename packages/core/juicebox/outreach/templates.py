"""A tiny, safe template language for outreach copy.

``{{ first_name }}``            -> value, or "" if missing
``{{ company | your team }}``   -> value, or the fallback after the pipe

No loops, no conditionals, no arbitrary expressions: recruiters paste these into
a form, so the renderer must not be able to execute anything.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from typing import Any

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*(?:\|\s*([^}]*?)\s*)?\}\}")
_LINK_RE = re.compile(r'href="(https?://[^"]+)"', re.IGNORECASE)


def used_variables(template: str) -> list[str]:
    return sorted({match.group(1) for match in _VAR_RE.finditer(template or "")})


def keep_known_variables(template: str, allowed: Iterable[str]) -> str:
    """Drop placeholders naming a variable that will never have a value.

    A generated draft can reference something the context does not carry — a
    model asked for a signature happily writes ``{{your_name}}`` whatever the
    prompt says — and ``render`` collapses an unknown name to "". That ships an
    email with a blank where a word should be. Rather than trusting the prompt,
    unknown names are resolved here: to their fallback text if one was given,
    otherwise removed.
    """
    if not template:
        return ""
    known = set(allowed)

    def substitute(match: re.Match[str]) -> str:
        name, fallback = match.group(1), match.group(2)
        if name in known:
            return match.group(0)
        return (fallback or "").strip()

    cleaned = _VAR_RE.sub(substitute, template)
    # Removing a placeholder that sat alone on its line leaves trailing spaces.
    return "\n".join(line.rstrip() for line in cleaned.splitlines())


def render(template: str, context: dict[str, Any]) -> str:
    """Substitute variables; unknown names collapse to their fallback or ''."""
    if not template:
        return ""

    def substitute(match: re.Match[str]) -> str:
        name, fallback = match.group(1), match.group(2)
        value = context.get(name)
        if value is None or value == "":
            return fallback or ""
        return str(value)

    return _VAR_RE.sub(substitute, template)


def missing_variables(template: str, context: dict[str, Any]) -> list[str]:
    """Variables with neither a value nor a fallback — worth warning about."""
    missing: list[str] = []
    for match in _VAR_RE.finditer(template or ""):
        name, fallback = match.group(1), match.group(2)
        value = context.get(name)
        if (value is None or value == "") and not fallback:
            missing.append(name)
    return sorted(set(missing))


def to_html(body: str) -> str:
    """Plain-text body -> minimal HTML, preserving paragraph breaks."""
    escaped = html.escape(body.strip())
    paragraphs = [p.strip().replace("\n", "<br>") for p in escaped.split("\n\n") if p.strip()]
    return "".join(f"<p>{p}</p>" for p in paragraphs)


def add_tracking(
    html_body: str, *, base_url: str, message_id: str, token: str
) -> str:
    """Rewrite links through the click endpoint and append an open pixel."""
    from urllib.parse import quote

    def rewrite(match: re.Match[str]) -> str:
        target = match.group(1)
        if f"{base_url}/t/" in target:
            return match.group(0)
        return (
            f'href="{base_url}/t/c/{message_id}?k={token}&u={quote(target, safe="")}"'
        )

    tracked = _LINK_RE.sub(rewrite, html_body)
    pixel = (
        f'<img src="{base_url}/t/o/{message_id}.png?k={token}" width="1" height="1" '
        'alt="" style="display:none">'
    )
    return tracked + pixel
