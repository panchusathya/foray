"""A tiny, safe template language for outreach copy.

``{{ first_name }}``            -> value, or "" if missing
``{{ company | your team }}``   -> value, or the fallback after the pipe

No loops, no conditionals, no arbitrary expressions: recruiters paste these into
a form, so the renderer must not be able to execute anything.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*(?:\|\s*([^}]*?)\s*)?\}\}")
# An AI directive: ``{{ai: personalize this, say why the role fits}}``. The
# sigil is deliberate — a bare multi-word placeholder is far more likely to be
# a typo'd variable than a request for prose, and a typo must stay a harmless
# blank rather than become a paid model call. Disjoint from _VAR_RE by
# construction (a variable name cannot contain a colon or a space), and
# ``[^{}]`` forbids nesting: the resolver receives the whole variable bag, so
# there is nothing to interpolate inside an instruction.
_AI_RE = re.compile(r"\{\{\s*ai\s*:\s*(?P<instruction>[^{}]+?)\s*\}\}", re.IGNORECASE)
_LINK_RE = re.compile(r'href="(https?://[^"]+)"', re.IGNORECASE)
# Recruiter-authored bodies may be the historical plain-text templates or the
# small HTML allowlist the sequence editor writes. Detection is conservative:
# only tags the editor actually emits flip us into HTML mode, so a literal
# "<script>" in an old template is still escaped by to_html().
_HTML_HINT_RE = re.compile(r"</?(?:p|br|strong|b|em|i|u|a)(?:\s|>|/)", re.IGNORECASE)
_ALLOWED_TAGS = frozenset({"p", "br", "strong", "b", "em", "i", "u", "a"})
_SKIP_CONTENT_TAGS = frozenset({"script", "style", "iframe", "object", "embed", "noscript"})
_SAFE_HREF_SCHEMES = frozenset({"http", "https"})


class MissingDirective(ValueError):
    """A directive had no resolved text. Raised, never defaulted: the only code
    allowed to remove a directive span is the code substituting real prose,
    otherwise an instruction meant for the model could reach a candidate."""


@dataclass(frozen=True)
class Directive:
    key: str
    instruction: str


def _directive_key(instruction: str) -> str:
    normalised = " ".join(instruction.split()).lower()
    return "ai:" + hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:12]


def used_directives(template: str) -> list[Directive]:
    """Every distinct AI directive in the template, in first-seen order."""
    seen: dict[str, Directive] = {}
    for match in _AI_RE.finditer(template or ""):
        instruction = match.group("instruction").strip()
        key = _directive_key(instruction)
        seen.setdefault(key, Directive(key=key, instruction=instruction))
    return list(seen.values())


def render_directives(template: str, resolved: dict[str, str]) -> str:
    """Replace each directive span with its resolved prose.

    Raises ``MissingDirective`` on any gap — there is deliberately no fallback
    branch here (see MissingDirective).
    """
    if not template:
        return ""

    def substitute(match: re.Match[str]) -> str:
        instruction = match.group("instruction").strip()
        key = _directive_key(instruction)
        text = resolved.get(key)
        if not text:
            raise MissingDirective(f"unresolved AI directive: {instruction[:80]}")
        return text

    return _AI_RE.sub(substitute, template)


def mark_directives(template: str) -> str:
    """Replace each directive with a visible ``[AI: instruction]`` marker.

    Preview-only: it shows the recruiter where model text will land when no
    model is available. The send path never uses this — sending fails closed.
    """
    return _AI_RE.sub(
        lambda match: f"[AI: {match.group('instruction').strip()}]", template or ""
    )


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


def looks_like_html(body: str) -> bool:
    """True when the body already uses the editor's HTML allowlist tags."""
    return bool(_HTML_HINT_RE.search(body or ""))


def _safe_href(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in _SAFE_HREF_SCHEMES or not parsed.netloc:
        return ""
    return raw


class _HtmlSanitizer(HTMLParser):
    """Keep p/br/strong/b/em/i/u/a[href]; unwrap or drop everything else."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._open: list[tuple[str, bool]] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip:
            if tag in _SKIP_CONTENT_TAGS:
                self._skip += 1
            return
        if tag in _SKIP_CONTENT_TAGS:
            self._skip = 1
            return
        if tag not in _ALLOWED_TAGS:
            return
        if tag == "br":
            self.parts.append("<br>")
            return
        if tag == "a":
            href = _safe_href(dict(attrs).get("href"))
            if not href:
                self._open.append(("a", False))
                return
            self.parts.append(f'<a href="{html.escape(href, quote=True)}">')
            self._open.append(("a", True))
            return
        self.parts.append(f"<{tag}>")
        self._open.append((tag, True))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip:
            if tag in _SKIP_CONTENT_TAGS:
                self._skip = max(0, self._skip - 1)
            return
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] != tag:
                continue
            while len(self._open) > index:
                opened, emitted = self._open.pop()
                if emitted:
                    self.parts.append(f"</{opened}>")
            return

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() != "br":
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        self.parts.append(html.escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        return

    def finish(self) -> str:
        self.close()
        while self._open:
            opened, emitted = self._open.pop()
            if emitted:
                self.parts.append(f"</{opened}>")
        return "".join(self.parts)


class _TextExtractor(HTMLParser):
    """HTML fragment -> readable plain text for the MIME text part."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip:
            if tag in _SKIP_CONTENT_TAGS:
                self._skip += 1
            return
        if tag in _SKIP_CONTENT_TAGS:
            self._skip = 1
            return
        if tag in {"p", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip:
            if tag in _SKIP_CONTENT_TAGS:
                self._skip = max(0, self._skip - 1)
            return
        if tag == "p":
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def finish(self) -> str:
        self.close()
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _sanitize_html(body: str) -> str:
    parser = _HtmlSanitizer()
    parser.feed(body)
    return parser.finish()


def sanitize_preview(body: str) -> str:
    """Strip disallowed tags so the editor preview can render HTML safely."""
    if looks_like_html(body):
        return _sanitize_html(body)
    return body or ""


def to_text(body: str) -> str:
    """Plain-text MIME part: strip tags when the body is HTML, else as-is."""
    if not body or not body.strip():
        return ""
    if not looks_like_html(body):
        return body.strip()
    parser = _TextExtractor()
    parser.feed(body)
    return parser.finish()


def to_html(body: str) -> str:
    """Body -> minimal HTML. HTML templates are sanitized; plain text is wrapped."""
    if not body or not body.strip():
        return ""
    if looks_like_html(body):
        return _sanitize_html(body)
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
