"""A posting from a link somebody pasted.

Every other posting in this system arrives from a source that already told us
the fields: Exa hands over a title and a company, a project upserts its own row,
a CSV has columns. A candidate texting "apply to this for me" hands over a URL
and nothing else, so this is the one path that has to read a page and decide
what it says.

**`schema.org/JobPosting` first, and mostly only.** A job board that wants to
appear in Google for Jobs publishes the structured block, which means most real
postings state their own title, company and location rather than making us
guess. The `<title>`/`og:` fallback exists for the ones that do not, and it is
deliberately thin: a wrong company on an application is not recoverable, so a
page we cannot read confidently produces *nothing* and the agent asks.

**Never a partial guess.** ``read_posting`` returns a title and a company or it
returns nothing. A row with a title and no employer would create an application
addressed to nobody, and the confirm message the agent sends -- "Head of
Finance at Ramp, that the one?" -- is only a real check if both halves came off
the page.
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enrich import taxonomy as tx
from ..models import JobPosting, utcnow
from .ids import source_id_for
from .sync import enqueue_parse

logger = logging.getLogger(__name__)

#: Provenance for a posting a candidate handed us, distinct from the four that
#: existed. It is the only source where the candidate, not the org, decided the
#: role is worth looking at -- which is why ``passes_bar`` is not consulted.
SOURCE_CANDIDATE_LINK = "candidate_link"

TIMEOUT_SECONDS = 10.0
MAX_BYTES = 600_000
MAX_JD_CHARS = 20_000

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

#: A URL in a text message.
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
#: Punctuation glued to the end of one. A person writes "apply to
#: https://x.test/jobs/1, thanks" and the comma is not part of the link; the
#: smart quotes are in the set because a phone keyboard produces them and a
#: curly close-quote stuck to a URL is a 404. Written as escapes rather than
#: literal characters so the source carries none that are ambiguous to read.
_TRAILING = re.compile(r"[.,;:!?)\]}'\"\u2019\u201d]+$")

_JSONLD = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_OG_TITLE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE
)
_OG_SITE = re.compile(
    r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE
)
_TAGS = re.compile(r"<[^>]+>")
_SCRIPTS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)

#: How a board writes "Role at Company" in a page title. The separator set is
#: what every ATS actually uses; anything else falls through to og:site_name.
_TITLE_SPLIT = re.compile(r"\s+(?:[-\u2013\u2014|\u00b7]|at)\s+")
#: Page titles that are a board's index rather than one posting. Compared with
#: a trailing "s" stripped, so "Job" and "Jobs" are one entry.
_NOT_A_ROLE = frozenset(
    {
        "job",
        "career",
        "opening",
        "open role",
        "open position",
        "vacancy",
        "vacancie",
        "position",
        "role",
        "work with u",
        "join u",
        "we are hiring",
        "we're hiring",
        "hiring",
        "apply",
        "search job",
        "all job",
    }
)

#: The same separator, left dangling once og:site_name's company is trimmed off
#: the end of a page title.
_TRAILING_SEP = re.compile(r"\s*(?:[-\u2013\u2014|\u00b7]|\bat\b)\s*$")


def urls_in(text: str) -> list[str]:
    """Every http(s) URL in a message, trailing punctuation trimmed."""
    return [_TRAILING.sub("", match.group(0)) for match in _URL.finditer(text or "")]


@dataclass(frozen=True)
class ReadPosting:
    """What a page said about itself. Empty ``company`` means unusable."""

    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    remote: bool = False
    jd_text: str = ""
    company_domain: str = ""

    def __bool__(self) -> bool:
        return bool(self.title and self.company)


def _text(value) -> str:
    return " ".join(htmllib.unescape(str(value or "")).split())


def _jsonld_nodes(page: str):
    for block in _JSONLD.findall(page)[:8]:
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                yield node
                graph = node.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)


def _types(node: dict) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).lower() for v in values if v}


def _location_of(node: dict) -> tuple[str, bool]:
    remote = str(node.get("jobLocationType") or "").upper() == "TELECOMMUTE"
    places = node.get("jobLocation")
    places = places if isinstance(places, list) else [places]
    for place in places:
        if not isinstance(place, dict):
            continue
        address = place.get("address")
        if isinstance(address, str):
            return _text(address), remote
        if not isinstance(address, dict):
            continue
        parts = [
            _text(address.get(key))
            for key in ("addressLocality", "addressRegion", "addressCountry")
        ]
        joined = ", ".join(p for p in parts if p)
        if joined:
            return joined, remote
    return ("Remote" if remote else ""), remote


def _from_jsonld(page: str, *, base: str) -> ReadPosting | None:
    for node in _jsonld_nodes(page):
        if "jobposting" not in _types(node):
            continue
        title = _text(node.get("title"))
        org = node.get("hiringOrganization")
        company = ""
        domain = ""
        if isinstance(org, dict):
            company = _text(org.get("name"))
            for key in ("url", "sameAs"):
                value = org.get(key)
                if isinstance(value, str) and value.strip():
                    host = urlparse(urljoin(base, value.strip())).netloc.lower()
                    if host:
                        domain = host.removeprefix("www.")
                        break
        elif isinstance(org, str):
            company = _text(org)
        if not title or not company:
            continue
        location, remote = _location_of(node)
        description = _text(_TAGS.sub(" ", str(node.get("description") or "")))
        return ReadPosting(
            url=base,
            title=title,
            company=company,
            location=location,
            remote=remote,
            jd_text=description[:MAX_JD_CHARS],
            company_domain=domain,
        )
    return None


def _from_meta(page: str, *, base: str) -> ReadPosting | None:
    """The thin fallback: "Role at Company" out of a page title.

    Only splits on a separator a real board uses, and only when both halves
    survive. Anything less certain returns nothing, because the agent asking
    "what's the company and role?" costs one message and a wrong employer on an
    application cannot be taken back.
    """
    heading = _OG_TITLE.search(page)
    raw = _text(heading.group(1)) if heading else ""
    if not raw:
        tag = _TITLE_TAG.search(page)
        raw = _text(tag.group(1)) if tag else ""
    if not raw:
        return None

    site = _OG_SITE.search(page)
    company = _text(site.group(1)) if site else ""
    if company and raw.lower().endswith(company.lower()):
        stem = raw[: len(raw) - len(company)].rstrip()
        title = _TRAILING_SEP.sub("", stem).strip() or raw
    else:
        parts = [p.strip() for p in _TITLE_SPLIT.split(raw) if p.strip()]
        if len(parts) < 2:
            return None
        title, company = parts[0], parts[-1]

    if not title or not company or len(title) < 3 or len(company) < 2:
        return None
    if title.strip().lower().rstrip("s") in _NOT_A_ROLE:
        # "Jobs at Acme" and "Careers | Acme" are a board's index page, not a
        # posting. The confirm would catch it -- the candidate would say no --
        # but only after a junk row with the title "Jobs" is in the database
        # and matching against everybody.
        return None
    body = _text(_TAGS.sub(" ", _SCRIPTS.sub(" ", page)))
    return ReadPosting(
        url=base,
        title=title[:240],
        company=company[:240],
        jd_text=body[:MAX_JD_CHARS],
    )


def read_posting(url: str, *, client: httpx.Client | None = None) -> ReadPosting | None:
    """Fetch a URL and read the posting off it, or ``None``.

    ``None`` covers everything from a 404 to a page we could read but not
    understand, and the caller treats them identically -- it asks the candidate
    for the company and role rather than inventing either.
    """
    if not (url or "").startswith(("http://", "https://")):
        return None
    owns = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT_SECONDS, follow_redirects=True, headers=_HEADERS
    )
    try:
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            logger.info("could not read the pasted link %s: %s", url, exc)
            return None
        if response.status_code >= 400:
            logger.info("pasted link %s -> HTTP %s", url, response.status_code)
            return None
        page = response.text[:MAX_BYTES]
        base = str(response.url)
    finally:
        if owns:
            client.close()

    found = _from_jsonld(page, base=base) or _from_meta(page, base=base)
    if found is None:
        logger.info("pasted link %s carried no readable posting", url)
    return found


def upsert_from_url(
    db: Session, *, org_id: str, read: ReadPosting, source_url: str
) -> JobPosting:
    """Store what the page said, reusing the row if we already have it.

    The upsert shape is ``routers/job_postings.create_posting``'s, on purpose:
    the same ``source_id_for`` key means a role a recruiter already imported
    and a role a candidate pasted are one row, not two, and applying to it is
    still one application per posting.
    """
    now = utcnow()
    sid = source_id_for(
        url=source_url, company=read.company, title=read.title, location=read.location
    )
    posting = db.scalar(
        select(JobPosting).where(JobPosting.org_id == org_id, JobPosting.source_id == sid)
    )
    created = posting is None
    if posting is None:
        posting = JobPosting(
            org_id=org_id,
            source=SOURCE_CANDIDATE_LINK,
            source_id=sid,
            status="pending_parse",
            first_seen_at=now,
        )
        db.add(posting)

    # Never overwrite what a richer source already established. A candidate's
    # link to a role a recruiter imported should add the URL, not replace a
    # curated title with a scraped one.
    posting.title = posting.title or read.title[:240]
    posting.company = posting.company or read.company[:240]
    posting.location = posting.location or read.location[:200]
    posting.jd_text = posting.jd_text or read.jd_text
    posting.source_url = posting.source_url or source_url
    posting.company_domain = posting.company_domain or read.company_domain[:240]
    posting.open_to_remote = posting.open_to_remote or read.remote
    posting.title_normalized = tx.normalize_title(posting.title or "")
    posting.company_normalized = tx.normalize_company(posting.company or "")
    posting.last_seen_at = now
    if created:
        posting.status = "open"
    db.flush()
    enqueue_parse(db, posting)
    return posting
