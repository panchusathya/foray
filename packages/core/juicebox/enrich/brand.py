"""A company's logo and its colours, taken from the company's own pages.

No vendor and no key, and that is not a compromise -- **the logo already
contains the brand**. Clearbit's free logo API is gone, Brandfetch and logo.dev
both want a key, and every one of them would answer for fewer employers than
the posting page itself does: a Greenhouse or Lever board renders the
employer's own logo, and a `schema.org/JobPosting` block names
`hiringOrganization.logo` outright. So the ladder starts at the page we are
already about to link to.

Colours are quantised out of the logo bitmap. Ranked by saturation weighted by
coverage, with near-white and near-black dropped: a wordmark is mostly
background, and the most *common* pixel in a logo is almost never the brand
colour.

**A miss is cached.** ``fetched_at`` is stamped on failure as well as success,
with ``error`` set, because a company with no discoverable logo would otherwise
be refetched on every card -- the same reason ``AGENTS.md`` gives for stamping
``<provider>_checked_at`` on an enrichment miss.

``CompanyBrand`` is deliberately **not** org-scoped, unlike almost every table
in this schema. It holds publicly-fetched marketing assets keyed by a public
domain, carries nothing belonging to a tenant, and an ``org_id`` would mean
paying for the same fetch once per org for the same logo.
"""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..models import CompanyBrand, JobPosting, utcnow

logger = logging.getLogger(__name__)

#: Bumped when the extraction changes shape, so cached rows are recomputed
#: rather than silently serving a palette produced by older rules.
BRAND_VERSION = "brand-v1"

TIMEOUT_SECONDS = 8.0
MAX_HTML_BYTES = 600_000
#: A logo is a few KB to a couple of hundred. Anything larger is a hero image
#: somebody put in `og:image`, and downscaling a 4MB JPEG to 88px is waste.
MAX_LOGO_BYTES = 3 * 1024 * 1024

#: How long a resolved brand is trusted, and how long a miss is remembered.
#: A hit lasts because logos change on a scale of years; a miss expires sooner
#: because it is as often a blocked request as an absent logo.
HIT_TTL = timedelta(days=90)
MISS_TTL = timedelta(days=7)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,image/*",
}

#: Hosts that are never the employer. A logo scraped from one of these is the
#: aggregator's own mark, which would put LinkedIn's blue on a Pepsi card.
_NOT_THE_EMPLOYER = frozenset(
    {
        "linkedin.com",
        # An aggregator serves its images off a separate CDN host, so blocking
        # the site without blocking its CDN blocks nothing that matters.
        "licdn.com",
        "indeedassets.com",
        "gdcdn.com",
        "indeed.com",
        "glassdoor.com",
        "ziprecruiter.com",
        "monster.com",
        "dice.com",
        "simplyhired.com",
        "builtin.com",
        "wellfound.com",
        "angel.co",
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "myworkdayjobs.com",
        "workday.com",
        "smartrecruiters.com",
        "jobvite.com",
        "icims.com",
        "workable.com",
        "bamboohr.com",
        "taleo.net",
        "oraclecloud.com",
        "google.com",
        "gstatic.com",
        "googleusercontent.com",
    }
)

SOURCE_JSONLD = "jsonld"
SOURCE_OG_IMAGE = "og_image"
SOURCE_TOUCH_ICON = "touch_icon"
SOURCE_FAVICON = "favicon"


@dataclass(frozen=True)
class Brand:
    """What a renderer needs. Never the raw bitmap -- that stays on disk."""

    domain: str
    #: ``#rrggbb``. Empty when no usable colour came out of the logo.
    primary: str = ""
    accent: str = ""
    logo_path: str = ""
    source: str = ""

    def __bool__(self) -> bool:
        return bool(self.primary or self.logo_path)


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def relative_luminance(colour: str) -> float:
    """WCAG relative luminance of a ``#rrggbb``. 0 is black, 1 is white."""
    rgb = rgb_of(colour)
    if rgb is None:
        return 0.0
    channels = []
    for raw in rgb:
        c = raw / 255.0
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(one: str, two: str) -> float:
    """WCAG contrast between two ``#rrggbb`` colours, 1.0 to 21.0."""
    a, b = relative_luminance(one), relative_luminance(two)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def rgb_of(colour: str) -> tuple[int, int, int] | None:
    """``"#004b93"`` -> ``(0, 75, 147)``, or ``None`` if it is not one.

    Public because the card renderer blends against it. A private helper
    imported across a package boundary is a public helper with worse
    documentation.
    """
    text = (colour or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return None


def _saturation(rgb: tuple[int, int, int]) -> float:
    high, low = max(rgb), min(rgb)
    return 0.0 if high == 0 else (high - low) / high


def palette_from_logo(data: bytes, *, count: int = 2) -> list[str]:
    """The brand colours in a logo bitmap, strongest first.

    Ranked by saturation weighted by coverage rather than by coverage alone: a
    wordmark on white is mostly white, so the most common pixel in a logo is
    almost never the brand colour. Near-white and near-black are dropped for
    the same reason -- they are the page behind the mark and the text on it.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            # Flatten transparency onto white: a PNG logo is usually a coloured
            # mark on nothing, and counting alpha-zero pixels as black would
            # make every transparent logo come out black.
            image = image.convert("RGBA")
            flat = Image.new("RGB", image.size, (255, 255, 255))
            flat.paste(image, mask=image.split()[3])
            flat.thumbnail((160, 160))
            quantised = flat.quantize(colors=16, method=Image.Quantize.MEDIANCUT)
            palette = quantised.getpalette() or []
            counts = quantised.getcolors() or []
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.info("could not read a palette out of the logo: %s", exc)
        return []

    scored: list[tuple[float, str]] = []
    total = sum(n for n, _ in counts) or 1
    for pixels, index in counts:
        rgb = tuple(palette[index * 3 : index * 3 + 3])
        if len(rgb) != 3:
            continue
        luminance = relative_luminance(_hex(rgb))
        if luminance > 0.90 or luminance < 0.02:
            continue
        saturation = _saturation(rgb)
        coverage = pixels / total
        # Weighted so a small, vivid mark beats a large, washed-out field, but
        # a single stray pixel still cannot win.
        scored.append((saturation * (coverage**0.35), _hex(rgb)))

    scored.sort(reverse=True)
    chosen: list[str] = []
    for _, colour in scored:
        if colour in chosen:
            continue
        # Two near-identical shades are one colour as far as a card is
        # concerned, and picking both wastes the accent slot.
        if any(contrast_ratio(colour, taken) < 1.3 for taken in chosen):
            continue
        chosen.append(colour)
        if len(chosen) >= count:
            break
    return chosen


# ---------------------------------------------------------------------------
# Finding a logo
# ---------------------------------------------------------------------------


def _registrable(host: str) -> str:
    """``boards.greenhouse.io`` -> ``greenhouse.io``. Two labels, no PSL.

    Crude on purpose: this feeds a blocklist of aggregator hosts, and the
    alternative is a public-suffix dependency to get ``co.uk`` right. A
    ``foo.co.uk`` reads as ``co.uk`` here, which is not on the list, so it
    passes -- the failure mode is permissive, never a wrong logo.
    """
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def is_employer_host(url: str) -> bool:
    """False for aggregators and ATS vendors -- their logo is not the employer's."""
    host = urlparse(url or "").netloc.lower().split(":")[0]
    if not host:
        return False
    return _registrable(host) not in _NOT_THE_EMPLOYER


_JSONLD = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_OG_IMAGE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE
)
_OG_IMAGE_REVERSED = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.IGNORECASE
)
_TOUCH_ICON = re.compile(
    r'<link[^>]+rel=["\'][^"\']*apple-touch-icon[^"\']*["\'][^>]+href=["\']([^"\']+)',
    re.IGNORECASE,
)
#: Captures ``rel`` as well as ``href``, because every ``rel`` containing
#: "icon" matches this -- including the apple-touch-icon, which is why the
#: caller filters on the captured rel. A single regex excluding "apple-touch"
#: needs a negative lookahead over an attribute whose order is not guaranteed,
#: and still gets `rel="icon apple-touch-icon"` wrong.
_ICON = re.compile(
    r'<link[^>]+rel=["\']([^"\']*icon[^"\']*)["\'][^>]+href=["\']([^"\']+)', re.IGNORECASE
)


def _jsonld_nodes(html: str):
    for block in _JSONLD.findall(html)[:5]:
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict):
                yield node


def _as_url(value) -> str:
    """A JSON-LD image is a string, or an ImageObject, or a list of either."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _as_url(value.get("url") or value.get("contentUrl") or "")
    if isinstance(value, list):
        for item in value:
            found = _as_url(item)
            if found:
                return found
    return ""


def logo_candidates(html: str, *, base: str) -> list[tuple[str, str]]:
    """``[(source, absolute_url)]``, best first.

    JSON-LD leads because it is the page *stating* which image is the
    employer's logo rather than us inferring it from a meta tag that is just as
    often a stock photo of an office.
    """
    found: list[tuple[str, str]] = []

    for node in _jsonld_nodes(html):
        org = node.get("hiringOrganization")
        if isinstance(org, dict):
            url = _as_url(org.get("logo"))
            if url:
                found.append((SOURCE_JSONLD, url))
        if node.get("@type") in ("Organization", "Corporation"):
            url = _as_url(node.get("logo"))
            if url:
                found.append((SOURCE_JSONLD, url))

    for pattern in (_OG_IMAGE, _OG_IMAGE_REVERSED):
        match = pattern.search(html)
        if match:
            found.append((SOURCE_OG_IMAGE, match.group(1)))
            break

    match = _TOUCH_ICON.search(html)
    if match:
        found.append((SOURCE_TOUCH_ICON, match.group(1)))
    for rel, href in _ICON.findall(html):
        if "apple-touch" in rel.lower():
            continue
        found.append((SOURCE_FAVICON, href))
        break

    absolute: list[tuple[str, str]] = []
    seen = set()
    for source, url in found:
        resolved = urljoin(base, url.strip())
        if not resolved.startswith(("http://", "https://")) or resolved in seen:
            continue
        seen.add(resolved)
        absolute.append((source, resolved))
    return absolute


def employer_domain(html: str, *, base: str) -> str:
    """The employer's own domain, per the page's own JSON-LD.

    This is the other half of what the fetch buys, and it fills a column that
    until now was read by ``jobmatch/referrals.py`` and written by nobody --
    so warm-path matching has been running against a blank.
    """
    for node in _jsonld_nodes(html):
        org = node.get("hiringOrganization")
        if not isinstance(org, dict):
            continue
        for key in ("url", "sameAs"):
            candidate = _as_url(org.get(key))
            host = urlparse(urljoin(base, candidate)).netloc.lower().split(":")[0]
            if host and _registrable(host) not in _NOT_THE_EMPLOYER:
                return host.removeprefix("www.")
    host = urlparse(base).netloc.lower().split(":")[0]
    if host and _registrable(host) not in _NOT_THE_EMPLOYER:
        return host.removeprefix("www.")
    return ""


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def _fetch(client: httpx.Client, url: str) -> httpx.Response | None:
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        logger.debug("brand fetch failed for %s: %s", url, exc)
        return None
    if response.status_code >= 400:
        logger.debug("brand fetch %s -> HTTP %s", url, response.status_code)
        return None
    return response


def _fetch_logo(client: httpx.Client, candidates: list[tuple[str, str]]) -> tuple[str, bytes]:
    for source, url in candidates:
        response = _fetch(client, url)
        if response is None:
            continue
        data = response.content
        if not data or len(data) > MAX_LOGO_BYTES:
            continue
        if not response.headers.get("content-type", "").lower().startswith("image/"):
            continue
        return source, data
    return "", b""


def resolve(
    db: Session,
    posting: JobPosting,
    *,
    client: httpx.Client | None = None,
    refresh: bool = False,
) -> Brand | None:
    """The employer's brand for this posting, or ``None``.

    ``None`` is an ordinary answer, not a failure: plenty of postings are on
    hosts that block us or carry no logo, and the card renderer has a neutral
    variant for exactly that. Nothing here raises.
    """
    domain = (posting.company_domain or "").strip().lower()
    cached = _cached(db, domain) if domain else None
    if cached is not None and not refresh:
        return _from_row(cached)

    source_url = (posting.apply_url or posting.source_url or "").strip()
    if not source_url.startswith(("http://", "https://")):
        return None

    owns = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT_SECONDS, follow_redirects=True, headers=_HEADERS
    )
    try:
        page = _fetch(client, source_url)
        if page is None:
            return None
        base = str(page.url)
        html = page.text[:MAX_HTML_BYTES]

        found_domain = employer_domain(html, base=base)
        if found_domain and not posting.company_domain:
            posting.company_domain = found_domain[:240]
        domain = domain or found_domain
        if not domain:
            return None

        # Re-check the cache: the domain may only now be known.
        cached = _cached(db, domain)
        if cached is not None and not refresh:
            return _from_row(cached)

        candidates = [
            (source, url)
            for source, url in logo_candidates(html, base=base)
            if is_employer_host(url)
        ]
        source, data = _fetch_logo(client, candidates)
    finally:
        if owns:
            client.close()

    if not data:
        _remember(db, domain=domain, error="no logo found")
        return None

    palette = palette_from_logo(data)
    relpath = storage.brand_logo_relpath(domain=domain)
    storage.save_bytes(relpath, data)
    row = _remember(
        db,
        domain=domain,
        logo_path=relpath,
        palette=palette,
        source=source,
    )
    return _from_row(row)


def _cached(db: Session, domain: str) -> CompanyBrand | None:
    row = db.scalar(select(CompanyBrand).where(CompanyBrand.domain == domain))
    if row is None or row.brand_version != BRAND_VERSION:
        return None
    ttl = MISS_TTL if row.error else HIT_TTL
    if row.fetched_at is None or row.fetched_at + ttl < utcnow():
        return None
    return row


def _remember(
    db: Session,
    *,
    domain: str,
    logo_path: str = "",
    palette: list[str] | None = None,
    source: str = "",
    error: str = "",
) -> CompanyBrand:
    row = db.scalar(select(CompanyBrand).where(CompanyBrand.domain == domain))
    if row is None:
        row = CompanyBrand(domain=domain)
        db.add(row)
    row.logo_path = logo_path
    row.palette = list(palette or [])
    row.source = source
    row.error = error[:400]
    row.brand_version = BRAND_VERSION
    row.fetched_at = utcnow()
    db.flush()
    return row


def _from_row(row: CompanyBrand) -> Brand | None:
    if row.error and not row.logo_path:
        return None
    palette = [c for c in (row.palette or []) if isinstance(c, str)]
    brand = Brand(
        domain=row.domain,
        primary=palette[0] if palette else "",
        accent=palette[1] if len(palette) > 1 else "",
        logo_path=row.logo_path or "",
        source=row.source or "",
    )
    return brand or None
