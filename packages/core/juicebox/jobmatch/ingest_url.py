"""A job link a candidate sent, turned into a ``JobPosting`` we can apply from.

CANDIDATE_AGENT_PLAN §8 tiers submission and keeps browser automation of
arbitrary sites out of scope. This module is the *read* half only: fetch what
the link points at, record it as a posting, and — where the ATS publishes them
— capture the application form's own questions so ``docgen.answers`` can fill
the real form rather than our four standard guesses. Nothing here submits.

Two paths, tried in order:

* **A known ATS.** Greenhouse and Lever publish their postings (Greenhouse:
  the form questions too) on unauthenticated JSON endpoints. Free, stable,
  and the JSON names the employer — no model needed.
* **Any other page.** A plain GET, tags stripped. The employer and location
  are then read out of the text by the existing parse job
  (``parse.resolve_identity``), so this path stays useful with no API key —
  thinner, not broken.

Every fetch goes through one transport seam (`FetchTransport`), for the same
reason ``ExaClient`` has one: tests stand in for the network, and a candidate
retries a link by texting it again, not by our queue re-buying the failure.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enrich import taxonomy as tx
from ..models import JobPosting, utcnow
from .ids import normalize_url, source_id_for
from .scrape import LOCATION_MAX, TITLE_MAX, _fit
from .sync import enqueue_parse

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 15.0
#: Enough for any job page; a cap because a texted link can point anywhere.
MAX_FETCH_BYTES = 2_000_000
#: Below this, the "page" is a bot-wall or an empty shell, not a description.
MIN_JD_CHARS = 200

USER_AGENT = "juicebox-intake/1.0 (+job application preparation)"

SOURCE_CANDIDATE_LINK = "candidate"


class LinkUnreachable(RuntimeError):
    """The link could not become a posting. The message is candidate-safe:
    it may be echoed into a text, so it names what went wrong, never how."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    status: int
    content_type: str
    text: str


#: GET one URL, however the caller wants that done. Tests supply a dict-backed
#: fake; production uses ``_http_fetch``.
FetchTransport = Callable[[str], FetchedPage]


def _http_fetch(url: str) -> FetchedPage:
    try:
        with httpx.Client(
            timeout=FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        logger.info("link fetch failed for %s: %s", url, exc)
        raise LinkUnreachable("the page did not answer") from exc
    body = response.text[: MAX_FETCH_BYTES // 2]
    return FetchedPage(
        url=str(response.url),
        status=response.status_code,
        content_type=(response.headers.get("content-type") or "").lower(),
        text=body,
    )


def fetch_page(url: str, *, transport: FetchTransport | None = None) -> FetchedPage:
    page = (transport or _http_fetch)(url)
    if page.status >= 400:
        raise LinkUnreachable("the page did not open")
    return page


# ---------------------------------------------------------------------------
# form questions
# ---------------------------------------------------------------------------

#: EEO / self-identification questions. We record that the form asks them so
#: the candidate sees the whole form, but INTAKE_COMPLIANCE is explicit that
#: protected attributes are never collected or asserted — these are answered
#: by the candidate on the real form, never by us.
_PROTECTED_RE = re.compile(
    r"\b(gender|race|racial|ethnic|hispanic|latin[oax]|veteran|disabilit|"
    r"sexual orientation|transgender|lgbtq|pronoun|date of birth|religion|"
    r"marital|national origin)\b|\bage\b",
    re.IGNORECASE,
)


def is_protected_question(label: str) -> bool:
    return bool(_PROTECTED_RE.search(label or ""))


def _question(index: int, label: str, *, required: bool, kind: str, options: list[str]) -> dict:
    return {
        "key": f"form_q{index}",
        "question": label.strip(),
        "required": bool(required),
        # text | select | file. A file field is the resume or cover letter,
        # which the package generates as documents rather than answers.
        "kind": kind,
        "self_identify": is_protected_question(label),
        "options": options[:12],
    }


# ---------------------------------------------------------------------------
# known ATS adapters
# ---------------------------------------------------------------------------


def _fetch_json(url: str, transport: FetchTransport | None) -> dict[str, Any]:
    page = fetch_page(url, transport=transport)
    try:
        data = json.loads(page.text)
    except ValueError as exc:
        raise LinkUnreachable("the job board answered unreadably") from exc
    if not isinstance(data, dict):
        raise LinkUnreachable("the job board answered unreadably")
    return data


def _greenhouse_plan(url: str) -> tuple[str, str] | None:
    """``(board, job_id)`` when the URL is a Greenhouse posting."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [p for p in parsed.path.split("/") if p]
    # /{board}/jobs/{id}
    if (
        host in {"boards.greenhouse.io", "job-boards.greenhouse.io",
                 "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}
        and len(parts) >= 3
        and parts[1] == "jobs"
        and parts[2].isdigit()
    ):
        return parts[0], parts[2]
    return None


def _ingest_greenhouse(url: str, transport: FetchTransport | None) -> dict | None:
    plan = _greenhouse_plan(url)
    if plan is None:
        return None
    board, job_id = plan
    api_host = (
        "boards-api.eu.greenhouse.io" if ".eu.greenhouse.io" in url else "boards-api.greenhouse.io"
    )
    data = _fetch_json(
        f"https://{api_host}/v1/boards/{board}/jobs/{job_id}?questions=true", transport
    )
    questions: list[dict] = []
    for index, raw in enumerate(data.get("questions") or [], start=1):
        label = str(raw.get("label") or "").strip()
        if not label:
            continue
        fields = raw.get("fields") or [{}]
        field_type = str((fields[0] or {}).get("type") or "")
        kind = "file" if "file" in field_type else ("select" if "select" in field_type else "text")
        options = [
            str(v.get("label") or "").strip()
            for v in ((fields[0] or {}).get("values") or [])
            if isinstance(v, dict) and v.get("label")
        ]
        questions.append(_question(index, label, required=bool(raw.get("required")), kind=kind, options=options))
    location = str(((data.get("location") or {}).get("name")) or "")
    return {
        "company": str(data.get("company_name") or "") or board.replace("-", " ").title(),
        "title": str(data.get("title") or ""),
        "location": location,
        "jd_text": strip_html(str(data.get("content") or "")),
        "form_questions": questions,
    }


def _lever_plan(url: str) -> tuple[str, str, str] | None:
    """``(api_host, org, posting_id)`` when the URL is a Lever posting."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [p for p in parsed.path.split("/") if p]
    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and len(parts) >= 2:
        api = "api.eu.lever.co" if host == "jobs.eu.lever.co" else "api.lever.co"
        return api, parts[0], parts[1]
    return None


def _ingest_lever(url: str, transport: FetchTransport | None) -> dict | None:
    plan = _lever_plan(url)
    if plan is None:
        return None
    api_host, org, posting_id = plan
    data = _fetch_json(f"https://{api_host}/v0/postings/{org}/{posting_id}", transport)
    categories = data.get("categories") or {}
    description = str(data.get("descriptionPlain") or "").strip()
    for block in data.get("lists") or []:
        head = str(block.get("text") or "").strip()
        body = strip_html(str(block.get("content") or ""))
        if head or body:
            description += f"\n\n{head}\n{body}"
    return {
        "company": org.replace("-", " ").title(),
        "title": str(data.get("text") or ""),
        "location": str(categories.get("location") or ""),
        "jd_text": description.strip(),
        # Lever's public postings endpoint carries no custom questions; the
        # package answers the standard set and the portal shows the rest.
        "form_questions": [],
    }


_LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


def _workday_plan(url: str) -> str | None:
    """The cxs JSON URL when this is a Workday posting, else ``None``.

    A Workday job page is a JS shell — ``strip_html`` of it is zero visible
    characters (measured live, 2026-08-27: a pasted Workday link got "couldn't
    open the job link" twice for exactly this). The shell's own data source is
    public and unauthenticated:
    ``https://<tenant>.wd<N>.myworkdayjobs.com/<locale?>/<site>/job/<...>/<slug>``
    maps to ``https://<host>/wday/cxs/<tenant>/<site>/job/<slug>``.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    if ".myworkdayjobs.com" not in host:
        return None
    tenant = host.split(".")[0]
    parts = [p for p in parsed.path.split("/") if p]
    if parts and _LOCALE_RE.match(parts[0]):
        parts = parts[1:]
    # <site>/job/<location-ish...>/<slug>
    if len(parts) < 3 or parts[1] != "job":
        return None
    site, slug = parts[0], parts[-1]
    return f"https://{host}/wday/cxs/{tenant}/{site}/job/{slug}"


def _ingest_workday(url: str, transport: FetchTransport | None) -> dict | None:
    cxs = _workday_plan(url)
    if cxs is None:
        return None
    data = _fetch_json(cxs, transport)
    info = data.get("jobPostingInfo") or {}
    if not isinstance(info, dict) or not str(info.get("title") or "").strip():
        raise LinkUnreachable("the job board answered unreadably")
    company = str((data.get("hiringOrganization") or {}).get("name") or "").strip()
    return {
        "company": company or _company_from_host(url) or "the company",
        "title": str(info.get("title") or ""),
        "location": str(info.get("location") or ""),
        "jd_text": strip_html(str(info.get("jobDescription") or "")),
        # Workday's form questions sit behind the account wall; the package
        # answers the standard set and the browser adapter meets the rest.
        "form_questions": [],
    }


#: Order matters only in that the first match wins; each adapter refuses URLs
#: that are not its own, so a new ATS is one function appended here.
_ATS_ADAPTERS: tuple[Callable[[str, FetchTransport | None], dict | None], ...] = (
    _ingest_greenhouse,
    _ingest_lever,
    _ingest_workday,
)


# ---------------------------------------------------------------------------
# any other page
# ---------------------------------------------------------------------------

_TAG_BLOCKS_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](og:title|og:site_name)[\"'][^>]*"
    r"content=[\"']([^\"']*)[\"']",
    re.IGNORECASE,
)
#: careers.example.com / jobs.example.co.uk -> "example"
_HOST_NOISE = ("www", "jobs", "careers", "boards", "apply", "job", "work", "hire")


def strip_html(markup: str) -> str:
    text = html_lib.unescape(markup or "")
    text = _TAG_BLOCKS_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"[ \t]*\n[ \t]*", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def _company_from_host(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    labels = [p for p in host.split(".") if p and p not in _HOST_NOISE]
    return labels[0].replace("-", " ").title() if len(labels) >= 2 else ""


_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _jsonld_location(node: dict) -> str:
    places = node.get("jobLocation")
    if isinstance(places, dict):
        places = [places]
    for place in places or []:
        if not isinstance(place, dict):
            continue
        address = place.get("address")
        if isinstance(address, dict):
            parts = [
                str(address.get(key) or "").strip()
                for key in ("addressLocality", "addressRegion", "addressCountry")
            ]
            joined = ", ".join(part for part in parts if part)
            if joined:
                return joined
        if str(place.get("name") or "").strip():
            return str(place["name"]).strip()
    return ""


def _jsonld_posting(page: FetchedPage) -> dict | None:
    """A schema.org JobPosting the page itself declares, or ``None``.

    The standard, not a vendor quirk: Ashby's React shell (which strips to ~30
    visible characters) ships one, and so do plenty of boards and aggregators.
    Reading it beats both the meta-tag heuristics (cleaner JD than a whole-page
    strip) and the model fallback (deterministic, free, byte-exact) — the page
    is *stating* the posting, so we take its word verbatim.
    """
    for match in _JSONLD_RE.finditer(page.text):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        queue = data if isinstance(data, list) else [data]
        index = 0
        while index < len(queue):
            node = queue[index]
            index += 1
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            if isinstance(graph, list):
                queue.extend(item for item in graph if isinstance(item, dict))
            if str(node.get("@type") or "").strip().lower() != "jobposting":
                continue
            title = str(node.get("title") or "").strip()
            jd_text = strip_html(str(node.get("description") or ""))
            if not title or len(jd_text) < MIN_JD_CHARS:
                continue
            org = node.get("hiringOrganization")
            company = str(
                (org.get("name") if isinstance(org, dict) else org) or ""
            ).strip()
            return {
                "company": company or _company_from_host(page.url),
                "title": title,
                "location": _jsonld_location(node),
                "jd_text": jd_text,
                "form_questions": [],
            }
    return None


def _ingest_generic(url: str, transport: FetchTransport | None) -> dict:
    page = fetch_page(url, transport=transport)
    if "html" not in page.content_type and "text" not in page.content_type:
        raise LinkUnreachable("the link is not a web page")

    declared = _jsonld_posting(page)
    if declared is not None:
        return declared

    meta = {key.lower(): value for key, value in _META_RE.findall(page.text)}
    title_match = _TITLE_RE.search(page.text)
    raw_title = html_lib.unescape(meta.get("og:title") or (title_match.group(1) if title_match else ""))
    company = html_lib.unescape(meta.get("og:site_name") or "") or _company_from_host(page.url)

    # "Senior Widget Engineer - Initech" is the commonest <title> shape; take
    # the role half and, when we still need one, the company half.
    title = raw_title.strip()
    # Hyphen, en dash, em dash, pipe; the dashes as escapes so a reviewer can
    # tell them apart.
    for separator in (" - ", " \u2013 ", " \u2014 ", " | "):
        if separator in title:
            head, _, tail = title.partition(separator)
            title = head.strip()
            company = company or tail.strip()
            break

    text = strip_html(page.text)
    if len(text) < MIN_JD_CHARS:
        # Too thin to be a job page as markup — most often a JS shell whose
        # real content sits in script-embedded JSON (the Workday shell strips
        # to zero characters). A model can read the raw HTML wherever the
        # content lives; refusing a link the candidate is looking at in their
        # own browser reads as broken, so this runs before we ever say
        # "couldn't open it".
        extracted = _llm_extract(page)
        if extracted is not None:
            return extracted
        raise LinkUnreachable("the page didn't have a readable job description")
    return {
        "company": company,
        "title": title,
        "location": "",
        "jd_text": text,
        "form_questions": [],
    }


#: Floor for a model-extracted description: below this the model has found a
#: heading, not a job posting, and building a package on it would answer
#: "why this company" from nothing.
_MIN_LLM_JD_CHARS = 80


def _llm_extract(page: FetchedPage) -> dict | None:
    """A model's reading of the raw page, or ``None`` to fall through.

    Deterministic paths first, always: this runs only after every adapter
    refused the URL and the stripped markup came up empty. The output is
    validated rather than trusted — extraction claims about a page we hold
    are checkable in shape (a posting has a title and a real description),
    and a result that fails the shape falls back to the honest refusal.
    """
    from ..llm.client import get_llm
    from ..llm.prompts import (
        JOB_PAGE_PARSE_SCHEMA,
        JOB_PAGE_PARSE_SYSTEM,
        job_page_parse_prompt,
    )

    llm = get_llm()
    if not llm.available:
        return None
    try:
        result = llm.json(
            system=JOB_PAGE_PARSE_SYSTEM,
            prompt=job_page_parse_prompt(url=page.url, html=page.text),
            schema=JOB_PAGE_PARSE_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 - the canned refusal ships instead
        logger.warning("llm page extraction failed for %s: %s", page.url, exc)
        return None
    data = result.data or {}
    title = str(data.get("title") or "").strip()
    jd_text = str(data.get("jd_text") or "").strip()
    if not data.get("is_job_posting") or not title or len(jd_text) < _MIN_LLM_JD_CHARS:
        # Logged because a silent None here reads downstream as "couldn't open
        # the link" — a flaky model rejected a perfectly good Ashby page this
        # way and the diagnosis started from nothing.
        logger.info(
            "llm read %s as not-a-posting (is_job_posting=%r, title=%r, jd_chars=%d)",
            page.url,
            data.get("is_job_posting"),
            title[:60],
            len(jd_text),
        )
        return None
    return {
        "company": str(data.get("company") or "").strip() or _company_from_host(page.url),
        "title": title,
        "location": str(data.get("location") or "").strip(),
        "jd_text": jd_text,
        "form_questions": [],
    }


# ---------------------------------------------------------------------------
# the one door
# ---------------------------------------------------------------------------


def ingest_posting_from_url(
    db: Session,
    *,
    org_id: str,
    url: str,
    transport: FetchTransport | None = None,
    now=None,
) -> tuple[JobPosting, bool]:
    """``(posting, created)`` for the page a candidate's link points at.

    Upserts on the same ``(org_id, source_id)`` key the scraper uses, so a
    link to a posting Exa already found lands on the existing row — and the
    one-application-per-posting rule then sees them as the same role.
    """
    now = now or utcnow()
    cleaned = normalize_url(url)
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LinkUnreachable("that doesn't look like a link I can open")

    sid = source_id_for(url=cleaned)
    posting = db.scalar(
        select(JobPosting).where(JobPosting.org_id == org_id, JobPosting.source_id == sid)
    )
    if posting is not None and (posting.jd_text or "").strip():
        posting.last_seen_at = now
        db.flush()
        return posting, False

    data: dict | None = None
    for adapter in _ATS_ADAPTERS:
        data = adapter(url, transport)
        if data is not None:
            break
    if data is None:
        data = _ingest_generic(url, transport)
    if not (data.get("jd_text") or "").strip():
        raise LinkUnreachable("the page didn't have a readable job description")

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

    posting.source_url = cleaned
    # Same project guard as the Exa ingest: a mandate's own record outranks
    # whatever we read off the page.
    if posting.project_id is None:
        posting.company = _fit(data.get("company") or posting.company, TITLE_MAX)
        posting.company_normalized = _fit(tx.normalize_company(posting.company), TITLE_MAX)
        posting.title = _fit(data.get("title") or posting.title, TITLE_MAX)
        posting.title_normalized = tx.normalize_title(posting.title)
        posting.location = _fit(data.get("location") or posting.location, LOCATION_MAX)
        posting.jd_text = data.get("jd_text") or posting.jd_text
        posting.form_questions = list(data.get("form_questions") or [])
    posting.last_seen_at = now
    if posting.status == "stale":
        posting.status = "pending_parse"
    db.flush()
    enqueue_parse(db, posting)
    return posting, created
