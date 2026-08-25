"""Turn an inbound email into the words the candidate actually wrote.

What arrives from a mailbox is a whole document: the reply, the signature,
the quoted thread underneath it, and enough markup to render all three. The
CRM was storing that verbatim, so a one-line "not right now, thanks" showed
up as several kilobytes of divs with our own outreach quoted inside it.

Two consequences, and the second is worse than the cosmetic one:

* a recruiter opening the timeline could not read the reply, and
* ``classify_reply`` ran on the same blob -- so the intent was decided partly
  from *our* pitch, quoted back at us. "let me know if there is any interest"
  is our sentence, and it was being read as though the candidate wrote it.

The provider keeps the original: ``Message.provider_native_id`` points at it,
so nothing here is irreversible. We store what a person needs to read.
"""

from __future__ import annotations

import re

from ..outreach.templates import html_to_text

# Everything from the first of these onwards is quoted history. Anchored to a
# line start so a sentence that merely mentions one does not truncate a reply.
_QUOTE_START = re.compile(
    r"^\s*(?:"
    r"-{2,}\s*Original Message\s*-{2,}"
    r"|_{10,}"
    r"|From:\s*\S.*"
    r"|On\s.{0,200}?\bwrote:\s*$"
    r"|\S+@\S+\s+wrote:\s*$"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Only the RFC-standard signature delimiter. Gmail-style "Best, <name>" blocks
# are left alone on purpose -- there is no reliable way to tell a sign-off from
# the last sentence of a message, and cutting a real sentence off a candidate's
# reply is a worse failure than leaving a phone number in.
_SIGNATURE = re.compile(r"^-- \s*$", re.MULTILINE)

MAX_STORED = 20_000

_MARKUP = re.compile(
    r"<(?:div|p|br|span|table|tr|td|body|html|blockquote|meta)\b", re.IGNORECASE
)


def _looks_like_markup(body: str) -> bool:
    """Broader than the outbound check, which only knows the editor's tags."""
    return bool(_MARKUP.search(body or ""))


def strip_quoted(text: str) -> str:
    """The reply without the thread it is replying to."""
    match = _QUOTE_START.search(text or "")
    return (text[: match.start()] if match else (text or "")).strip()


def clean_inbound_body(body: str, *, is_html: bool = False) -> str:
    """Markup out, quoted history out, whitespace tidied.

    Falls back to the original whenever cleaning would leave nothing: an empty
    timeline entry is strictly worse than an ugly one, and a reply that is
    *only* a quote-shaped line is a real thing people send.
    """
    raw = body or ""
    if not raw.strip():
        return ""

    text = html_to_text(raw) if (is_html or _looks_like_markup(raw)) else raw
    if not text.strip():
        text = raw

    trimmed = strip_quoted(text)
    if not trimmed.strip():
        trimmed = text

    sig = _SIGNATURE.search(trimmed)
    if sig and trimmed[: sig.start()].strip():
        trimmed = trimmed[: sig.start()]

    collapsed = re.sub(r"\n{3,}", "\n\n", trimmed).strip()
    return collapsed[:MAX_STORED] if collapsed else raw.strip()[:MAX_STORED]
