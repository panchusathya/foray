"""vCard 3.0 for a job match, rendered as an iMessage contact card.

The projection is ``jobcard()``; this module is only the wire format. FN is
what iMessage paints large (title · company). The candidate never sees a
score — ``playful_note`` reads reasons and location only.
"""

from __future__ import annotations

import base64
import re
from typing import Any

_NOTE_BUDGET = 200
_FOLD_OCTETS = 75
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def playful_note(card: dict[str, Any]) -> str:
    """Deterministic canned copy from the projection. ≲200 chars, never a score."""
    reasons = [str(r).strip() for r in (card.get("reasons") or []) if str(r).strip()][:2]
    location = str(card.get("location") or "").strip()
    tap = "👍 tap back a thumbs-up and i'll apply for you"
    why = f"✨ why you: {' · '.join(reasons)}" if reasons else ""
    loc = f"📍 {location}" if location else ""
    lines = [part for part in (why, loc, tap) if part]
    note = "\n".join(lines)
    if len(note) <= _NOTE_BUDGET:
        return note
    # Drop the second reason first; then the why-line; never the tapback cue.
    if len(reasons) == 2:
        return playful_note({**card, "reasons": reasons[:1]})
    if why:
        return playful_note({**card, "reasons": []})
    if loc:
        return playful_note({**card, "location": ""})
    return tap[:_NOTE_BUDGET]


def vcard_filename(card: dict[str, Any]) -> str:
    """``{company}-{title}.vcf``, filesystem-safe."""
    company = _slug(str(card.get("company") or ""))
    title = _slug(str(card.get("title") or ""))
    stem = "-".join(part for part in (company, title) if part) or "role"
    return f"{stem[:80]}.vcf"


def build_vcard(card: dict[str, Any], *, photo_png: bytes) -> bytes:
    """vCard 3.0, CRLF, RFC 75-octet folding (PHOTO especially)."""
    title = str(card.get("title") or "").strip()
    company = str(card.get("company") or "").strip()
    fn = " · ".join(part for part in (title, company) if part) or "role"
    url = str(card.get("url") or "").strip()
    note = playful_note(card)

    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"FN:{_escape(fn)}",
        f"N:{_escape(title)};;;;",
    ]
    if company:
        lines.append(f"ORG:{_escape(company)}")
    if title:
        lines.append(f"TITLE:{_escape(title)}")
    if url:
        lines.append(f"URL:{_escape(url)}")
    if note:
        lines.append(f"NOTE:{_escape(note)}")
    if photo_png:
        b64 = base64.b64encode(photo_png).decode("ascii")
        lines.append(f"PHOTO;ENCODING=b;TYPE=PNG:{b64}")
    lines.append("END:VCARD")
    folded = [_fold(line) for line in lines]
    return ("\r\n".join(folded) + "\r\n").encode("utf-8")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", value.lower()).strip("-")


def _escape(value: str) -> str:
    """vCard 3.0 text escaping: ``\\``, newline, ``;``, ``,``."""
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _fold(line: str) -> str:
    """RFC 2426 §2.6: fold at 75 octets, continuation lines start with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= _FOLD_OCTETS:
        return line
    chunks: list[bytes] = []
    while raw:
        limit = _FOLD_OCTETS if not chunks else _FOLD_OCTETS - 1
        chunk = raw[:limit]
        while chunk and (chunk[-1] & 0xC0) == 0x80:
            chunk = chunk[:-1]
        if not chunk:
            chunk = raw[:1]
        chunks.append(chunk)
        raw = raw[len(chunk) :]
    first, *rest = (c.decode("utf-8") for c in chunks)
    return first + "".join("\r\n " + piece for piece in rest)
