"""Stable source_id / spec_version helpers for job postings."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse, urlunparse

from ..enrich import taxonomy as tx
from ..models import JobPosting


def normalize_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", "", ""))


def source_id_for(
    *,
    url: str = "",
    company: str = "",
    title: str = "",
    location: str = "",
    project_id: str = "",
) -> str:
    if project_id:
        return f"project:{project_id}"[:64]
    normalised = normalize_url(url)
    if normalised:
        return hashlib.sha1(normalised.encode("utf-8")).hexdigest()
    key = "|".join(
        (
            tx.normalize_company(company),
            tx.normalize_title(title),
            tx.normalize_location(location),
        )
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def parse_fingerprint(posting: JobPosting) -> str:
    blob = "|".join(
        (
            posting.title or "",
            posting.location or "",
            posting.jd_text or "",
            posting.seniority or "",
        )
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def spec_version_for(parsed_spec: dict[str, Any]) -> str:
    encoded = json.dumps(parsed_spec, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()
