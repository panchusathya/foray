"""Which ATS is behind a posting, and where its application form actually is.

Two questions, and they are not the same one. ``detect`` is pure: given a URL,
which vendor's form is this and what are the ids inside it. ``resolve_target``
is the I/O half: a posting's ``source_url`` came from Exa and is very often an
aggregator listing or a LinkedIn page rather than the form itself, so it
follows one hop to find the real thing and caches the answer on the row.

**A vendor we recognise is not a vendor we can submit to.** The tier here says
what kind of submission the vendor *admits* (documented API, browser form,
an application address); whether an adapter exists is
``applications.submit.registry``'s question. Unknown vendor is not a failure
either -- it is Tier 0, the assisted hand-off, which is what we already do for
everything (CANDIDATE_AGENT_PLAN §8).

The follow step is deliberately one hop and deliberately forgiving, for the
same reason ``jobmatch/liveness.py`` only counts an explicit 404: aggregators
answer 403 to a datacentre IP all day, and treating that as "no ATS" is
invisible when it is wrong.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy.orm import Session

from ...models import JobPosting, utcnow

logger = logging.getLogger(__name__)

# CANDIDATE_AGENT_PLAN §8. The tier is a property of the vendor, not of our
# coverage: it says what the vendor admits, and the submitter registry says
# what we have actually built.
TIER_HANDOFF = 0
TIER_EMAIL = 1
TIER_API = 2
TIER_BROWSER = 3

VENDOR_EMAIL = "email"
VENDOR_GREENHOUSE = "greenhouse"
VENDOR_LEVER = "lever"
VENDOR_ASHBY = "ashby"
VENDOR_SMARTRECRUITERS = "smartrecruiters"
VENDOR_WORKDAY = "workday"
VENDOR_ORACLE = "oracle"
VENDOR_ICIMS = "icims"
VENDOR_TALEO = "taleo"
VENDOR_JOBVITE = "jobvite"
VENDOR_BAMBOOHR = "bamboohr"
VENDOR_WORKABLE = "workable"
VENDOR_YC = "yc"


@dataclass(frozen=True)
class ApplyTarget:
    """Where an application goes, and by what means.

    ``apply_url`` is a ``mailto:`` for ``VENDOR_EMAIL`` and an https form URL
    for everything else. ``tenant`` is the employer's own namespace inside the
    vendor (a Greenhouse board token, a Workday subdomain) -- browser adapters
    need it to know which portal account to reuse.
    """

    vendor: str = ""
    apply_url: str = ""
    external_job_id: str = ""
    tenant: str = ""
    tier: int = TIER_HANDOFF

    def __bool__(self) -> bool:
        return bool(self.vendor)


@dataclass(frozen=True)
class _Pattern:
    vendor: str
    tier: int
    #: Matched against the lowercased netloc. May capture ``tenant``.
    host: re.Pattern[str]
    #: Matched against path + query. May capture ``tenant`` and ``job_id``.
    #: When ``required`` the URL is not claimed for this vendor without a hit --
    #: that is what stops a marketing page on an oraclecloud.com host from
    #: reading as an application form.
    locator: re.Pattern[str] | None = None
    required: bool = False


_HEX_ID = r"[0-9a-fA-F][0-9a-fA-F-]{19,}"

# Order matters only where two hosts could both match; they do not today.
PATTERNS: tuple[_Pattern, ...] = (
    # Greenhouse has three shapes: the hosted board, the newer job-boards host,
    # and the iframe embed a company's own careers page serves. The embed is the
    # one that carries the board token in `for=`.
    _Pattern(
        vendor=VENDOR_GREENHOUSE,
        tier=TIER_API,
        host=re.compile(r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io$"),
        locator=re.compile(
            r"^/embed/job_app\?token=(?P<job_id>\d+)(?:&(?:amp;)?for=(?P<tenant>[\w-]+))?"
        ),
        required=True,
    ),
    _Pattern(
        vendor=VENDOR_GREENHOUSE,
        tier=TIER_API,
        host=re.compile(r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io$"),
        locator=re.compile(r"^/(?P<tenant>[\w.-]+)/jobs/(?P<job_id>\d+)"),
        required=True,
    ),
    _Pattern(
        vendor=VENDOR_LEVER,
        tier=TIER_API,
        host=re.compile(r"^jobs\.(?:eu\.)?lever\.co$"),
        locator=re.compile(rf"^/(?P<tenant>[\w.-]+)/(?P<job_id>{_HEX_ID})"),
        required=True,
    ),
    _Pattern(
        vendor=VENDOR_ASHBY,
        tier=TIER_API,
        host=re.compile(r"^jobs\.ashbyhq\.com$"),
        locator=re.compile(rf"^/(?P<tenant>[\w.-]+)/(?P<job_id>{_HEX_ID})"),
        required=True,
    ),
    _Pattern(
        vendor=VENDOR_SMARTRECRUITERS,
        tier=TIER_API,
        host=re.compile(r"^(?:jobs|careers)\.smartrecruiters\.com$"),
        locator=re.compile(r"^/(?P<tenant>[\w.-]+)/(?P<job_id>\d+)"),
        required=True,
    ),
    _Pattern(
        vendor=VENDOR_WORKDAY,
        tier=TIER_BROWSER,
        host=re.compile(r"^(?P<tenant>[\w-]+)\.(?:wd\d+\.)?myworkday(?:jobs|site)\.com$"),
        # JR-0012345 / R12345 / REQ_123 at the tail of a /job/ path.
        locator=re.compile(r"/job/.*?(?P<job_id>(?:JR|R|REQ)[-_]?\d+)", re.IGNORECASE),
    ),
    _Pattern(
        vendor=VENDOR_ORACLE,
        tier=TIER_BROWSER,
        host=re.compile(r"^(?P<tenant>[\w-]+)\..*oraclecloud\.com$"),
        locator=re.compile(
            r"/hcmUI/CandidateExperience/[\w-]+/sites/(?P<site>[\w-]+)"
            r"/(?:job|requisitions)/(?P<job_id>\d+)",
            re.IGNORECASE,
        ),
        required=True,
    ),
    _Pattern(
        vendor=VENDOR_ICIMS,
        tier=TIER_BROWSER,
        host=re.compile(r"^(?P<tenant>[\w-]+)\.icims\.com$"),
        locator=re.compile(r"/jobs/(?P<job_id>\d+)"),
    ),
    _Pattern(
        vendor=VENDOR_TALEO,
        tier=TIER_BROWSER,
        host=re.compile(r"^(?P<tenant>[\w-]+)\.taleo\.net$"),
    ),
    _Pattern(
        vendor=VENDOR_JOBVITE,
        tier=TIER_BROWSER,
        host=re.compile(r"^(?:jobs|careers)\.jobvite\.com$"),
        locator=re.compile(r"^/(?P<tenant>[\w.-]+)/job/(?P<job_id>[\w-]+)"),
        required=True,
    ),
    _Pattern(
        vendor=VENDOR_BAMBOOHR,
        tier=TIER_BROWSER,
        host=re.compile(r"^(?P<tenant>[\w-]+)\.bamboohr\.com$"),
        locator=re.compile(r"/(?:careers|jobs/view\.php\?id=)/?(?P<job_id>\d+)"),
    ),
    _Pattern(
        vendor=VENDOR_WORKABLE,
        tier=TIER_BROWSER,
        host=re.compile(r"^(?:apply|jobs)\.workable\.com$"),
        locator=re.compile(r"^/(?P<tenant>[\w.-]+)/j/(?P<job_id>[\w-]+)"),
        required=True,
    ),
    # YC job pages. Both hosts show the same posting; applying happens on
    # workatastartup.com behind a candidate account (browser/yc.py). `required`
    # is what keeps company profiles (`/companies/nessie`) and WaaS listing
    # pages (`/jobs/l/software-engineer`) out: neither is a posting, and both
    # are in the book today. The company slug is the tenant so the recruiter
    # views can name the employer even when the scrape left `company` blank.
    _Pattern(
        vendor=VENDOR_YC,
        tier=TIER_BROWSER,
        host=re.compile(r"^(?:www\.)?ycombinator\.com$"),
        locator=re.compile(r"^/companies/(?P<tenant>[\w-]+)/jobs/(?P<job_id>[\w-]+)"),
        required=True,
    ),
    _Pattern(
        vendor=VENDOR_YC,
        tier=TIER_BROWSER,
        host=re.compile(r"^(?:www\.)?workatastartup\.com$"),
        locator=re.compile(r"^/jobs/(?P<job_id>\d+)"),
        required=True,
    ),
)

#: Hosts we follow rather than claim. A LinkedIn or Indeed URL is a pointer to
#: an application form, never one itself.
AGGREGATOR_HOSTS: tuple[str, ...] = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "builtin.com",
    "wellfound.com",
    "angel.co",
    "dice.com",
    "simplyhired.com",
    "monster.com",
    "talent.com",
    "adzuna.com",
    "jooble.org",
    "otta.com",
    "lensa.com",
    "startup.jobs",
    "remoterocketship.com",
    "levels.fyi",
)

# Same reasoning as jobmatch/liveness.py's headers: a bare client is blocked by
# boards that serve a browser happily, and being blocked must not read to us as
# "this posting has no ATS".
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}
# One hop, inline, while a package build waits on it.
TIMEOUT_SECONDS = 8.0
#: Read cap. A careers page is tens of KB; anything past this is not one.
MAX_BYTES = 600_000

_EMAIL = r"[\w.+-]+@[\w-]+\.[\w.-]+"
#: Only an address the copy actually points at. A bare address anywhere in a JD
#: is as likely to be a hiring manager's as an application inbox, and mailing a
#: resume to the wrong person is not recoverable.
_APPLY_EMAIL = re.compile(
    rf"(?:apply|applications?|send|submit|email|resum|cv)\b[^\n]{{0,80}}?({_EMAIL})",
    re.IGNORECASE,
)
_MAILTO = re.compile(rf"mailto:({_EMAIL})", re.IGNORECASE)


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().split(":")[0]
    except ValueError:
        return ""


def is_aggregator(url: str) -> bool:
    host = _host(url)
    return any(host == agg or host.endswith("." + agg) for agg in AGGREGATOR_HOSTS)


def detect_target(url: str) -> ApplyTarget:
    """Vendor, tenant and job id from a URL alone. No I/O, never guesses."""
    raw = (url or "").strip()
    if not raw:
        return ApplyTarget()
    if raw.lower().startswith("mailto:"):
        found = _MAILTO.search(raw)
        if found is None:
            return ApplyTarget()
        return ApplyTarget(
            vendor=VENDOR_EMAIL, apply_url=f"mailto:{found.group(1)}", tier=TIER_EMAIL
        )
    if not raw.startswith(("http://", "https://")):
        return ApplyTarget()

    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower().split(":")[0]
    locator_subject = parsed.path + (f"?{parsed.query}" if parsed.query else "")

    for pattern in PATTERNS:
        host_match = pattern.host.match(host)
        if host_match is None:
            continue
        found = pattern.locator.search(locator_subject) if pattern.locator else None
        if pattern.required and found is None:
            continue
        groups = found.groupdict() if found else {}
        tenant = groups.get("tenant") or _group(host_match, "tenant") or ""
        # Oracle's portal identity is the pod host plus the site inside it, and
        # a candidate account is scoped to both -- one label alone would make
        # two employers on the same pod look like one tenant.
        site = groups.get("site") or ""
        if site and site != tenant:
            tenant = f"{tenant}/{site}" if tenant else site
        return ApplyTarget(
            vendor=pattern.vendor,
            apply_url=raw,
            external_job_id=groups.get("job_id") or "",
            tenant=tenant,
            tier=pattern.tier,
        )
    return ApplyTarget()


def _group(match: re.Match[str], name: str) -> str:
    """A named group that may not exist in this pattern at all."""
    return match.groupdict().get(name) or ""


def apply_email(text: str) -> str:
    """An application address the copy points at, or empty."""
    found = _MAILTO.search(text or "") or _APPLY_EMAIL.search(text or "")
    return found.group(1).strip(".,;:") if found else ""


def _vendor_links(html: str, *, base: str) -> ApplyTarget:
    """The first ATS URL a page points at: JSON-LD applyUrl, canonical, href."""
    for candidate in _candidate_urls(html, base=base):
        target = detect_target(candidate)
        if target:
            return target
    return ApplyTarget()


def _candidate_urls(html: str, *, base: str) -> list[str]:
    urls: list[str] = []
    # JSON-LD first: a JobPosting block that names its own applyUrl is the
    # page telling us the answer rather than us inferring it.
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )[:5]:
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            for key in ("applyUrl", "url", "sameAs"):
                value = node.get(key)
                if isinstance(value, str):
                    urls.append(value)
    canonical = re.search(
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', html, re.IGNORECASE
    )
    if canonical:
        urls.append(canonical.group(1))
    urls.extend(re.findall(r'href=["\']([^"\']+)["\']', html)[:400])
    return [urljoin(base, url) for url in urls if url]


def follow(url: str, *, client: httpx.Client | None = None) -> ApplyTarget:
    """One hop: read the page and take the first ATS link it points at."""
    owns = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT_SECONDS, follow_redirects=True, headers=_HEADERS
    )
    try:
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            logger.debug("could not read %s to find its ATS: %s", url, exc)
            return ApplyTarget()
        if response.status_code >= 400:
            logger.debug("no ATS from %s: HTTP %s", url, response.status_code)
            return ApplyTarget()
        # A redirect may have landed us on the form itself.
        landed = detect_target(str(response.url))
        if landed:
            return landed
        return _vendor_links(response.text[:MAX_BYTES], base=str(response.url))
    finally:
        if owns:
            client.close()


def target_of(posting: JobPosting) -> ApplyTarget:
    """The cached answer, without touching the network."""
    if not posting.ats_vendor:
        return ApplyTarget()
    tier = TIER_EMAIL if posting.ats_vendor == VENDOR_EMAIL else _tier_for(posting.ats_vendor)
    return ApplyTarget(
        vendor=posting.ats_vendor,
        apply_url=posting.apply_url or posting.source_url,
        external_job_id=posting.ats_external_job_id,
        tenant=posting.ats_tenant,
        tier=tier,
    )


def _tier_for(vendor: str) -> int:
    for pattern in PATTERNS:
        if pattern.vendor == vendor:
            return pattern.tier
    return TIER_HANDOFF


def resolve_target(
    db: Session,
    posting: JobPosting,
    *,
    client: httpx.Client | None = None,
    refresh: bool = False,
    allow_network: bool = True,
    stamp_on_miss: bool = True,
) -> ApplyTarget:
    """Resolve, cache and return the apply target for one posting.

    Cached on the row because every surface wants it: the job card wants to say
    where applying goes, the submitter wants the vendor, and re-fetching an
    aggregator page per read would be a request per card.

    With ``allow_network=False`` this is pure URL classification -- what the
    parse path wants, since it runs on every posting we ingest and a fetch per
    posting is a fetch per posting. A miss there is not recorded as final, so
    the first submission attempt still gets to follow the link.

    ``stamp_on_miss=False`` makes a *networked* miss non-final too. The
    proposal gate resolves the handful of postings about to be carded, and an
    aggregator that 403s a datacentre IP must not permanently cache
    "unrecognised" on a posting a later attempt could resolve -- the same
    reason the 2026-08 backfill was never run. Success still stamps either
    way: a recognised vendor is a real answer.
    """
    if posting.ats_resolved_at is not None and not refresh:
        return target_of(posting)

    source = (posting.source_url or "").strip()
    target = detect_target(source)
    # Unrecognised is the common case, not the exception: an Exa result is a
    # listing far more often than a form. One hop, then we stop.
    if not target and source.startswith(("http://", "https://")) and allow_network:
        target = follow(source, client=client)
    if not target:
        address = apply_email(posting.jd_text or "")
        if address:
            target = ApplyTarget(
                vendor=VENDOR_EMAIL, apply_url=f"mailto:{address}", tier=TIER_EMAIL
            )

    if not target and not stamp_on_miss:
        # A miss the caller declared non-final: report it, write nothing. The
        # row keeps whatever it had, and the next resolution gets a clean try.
        return target
    posting.ats_vendor = target.vendor
    posting.ats_tenant = target.tenant[:120]
    posting.ats_external_job_id = target.external_job_id[:120]
    posting.apply_url = target.apply_url or source
    # YC listings routinely arrive with the employer only in the URL slug --
    # blank `company` makes every card and message read "at the company". The
    # slug is not a perfect name ("10x-science" -> "10X Science") but it is
    # the employer, which blank is not. Only ever fills a blank.
    if target.vendor == VENDOR_YC and target.tenant and not (posting.company or "").strip():
        posting.company = target.tenant.replace("-", " ").title()
        posting.company_normalized = posting.company.lower()
    # Only a resolution that was allowed its network hop counts as final. The
    # parse path resolves from the URL alone -- cheap, inline, and enough for
    # the majority that are already a form -- and leaving the stamp unset is
    # what lets the first real submission attempt do the follow.
    if target or allow_network:
        posting.ats_resolved_at = utcnow()
    db.flush()
    if target:
        logger.info(
            "posting %s resolves to %s (tier %s) %s",
            posting.id,
            target.vendor,
            target.tier,
            target.apply_url,
        )
    return target
