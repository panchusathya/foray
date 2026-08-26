"""Employer logo as a contact-card photo, or a deterministic monogram.

Google's s2 favicon endpoint is the only origin we fetch: ``company_domain``
is almost always empty on a posting, and ``apply_url`` / ``source_url`` hosts
are often ATS aggregators. Hitting those directly would paint every card with
Greenhouse, and fetching ``https://{domain}/favicon.ico`` would be SSRF.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from collections.abc import Callable
from functools import lru_cache
from urllib.parse import quote, urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont

from ..applications.ats.detect import AGGREGATOR_HOSTS
from ..models import JobPosting

logger = logging.getLogger(__name__)

#: Test seam, same shape as ``linq.MediaTransport``: url -> (status, body).
LogoTransport = Callable[[str], tuple[int, bytes]]

PHOTO_SIZE = 512
FETCH_TIMEOUT = 4.0
_GOOGLE_S2 = "https://www.google.com/s2/favicons?domain={domain}&sz=256"

# Suffixes whose favicon is the vendor, not the employer. Includes the
# aggregator list detect.py already maintains, plus the ATS board hosts.
_ATS_SUFFIXES: tuple[str, ...] = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "smartrecruiters.com",
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "oraclecloud.com",
    "icims.com",
    "taleo.net",
    "jobvite.com",
    "bamboohr.com",
    "workable.com",
    "linkedin.com",
    *AGGREGATOR_HOSTS,
)

_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_BAD_TLDS = frozenset({"local", "internal", "localhost", "lan", "home", "test"})
_NEAR_WHITE = 240


def posting_domain(posting: JobPosting) -> str:
    """A hostname we will fetch a logo for, or empty (use the monogram).

    ``company_domain`` is the happy path and is usually blank. The URL host
    is only used when it looks like an employer site, not an ATS or job board.
    """
    for raw in (
        posting.company_domain or "",
        _host_of(posting.source_url or ""),
        _host_of(posting.apply_url or ""),
    ):
        domain = sanitize_hostname(raw)
        if domain and not _is_ats_or_aggregator(domain):
            return domain
    return ""


def sanitize_hostname(raw: str) -> str:
    """Public DNS hostname, or empty. No IPs, no localhost, no dotted-quads."""
    host = (raw or "").strip().lower()
    if "://" in host:
        host = (urlparse(host).hostname or "").lower()
    host = host.split("@")[-1].split(":")[0].strip(".")
    host = host.removeprefix("www.")
    if not host or not _HOST_RE.match(host):
        return ""
    tld = host.rsplit(".", 1)[-1]
    if tld in _BAD_TLDS:
        return ""
    labels = host.split(".")
    if len(labels) == 4 and all(p.isdigit() for p in labels):
        return ""
    return host


def fetch_logo(domain: str, *, transport: LogoTransport | None = None) -> bytes | None:
    """Google s2 favicon bytes, or None. Never raises. 4s cap."""
    host = sanitize_hostname(domain)
    if not host:
        return None
    url = _GOOGLE_S2.format(domain=quote(host, safe=".-"))
    try:
        if transport is not None:
            status, body = transport(url)
            if status >= 400 or not body:
                return None
            return body
        response = httpx.get(url, timeout=FETCH_TIMEOUT, follow_redirects=True)
        if response.status_code >= 400 or not response.content:
            return None
        return response.content
    except Exception as exc:  # noqa: BLE001 - a miss is a monogram, not a 500
        logger.info("favicon fetch missed for %s: %s", host, exc)
        return None


def contact_photo(logo_png: bytes | None, *, company: str) -> bytes:
    """512x512 PNG. Garbage bytes and missing logos become a monogram."""
    try:
        if logo_png:
            logo = Image.open(io.BytesIO(logo_png)).convert("RGBA")
            return _compose_logo(logo, company=company)
    except Exception:  # noqa: BLE001 - unreadable bytes → monogram
        pass
    return _monogram(company)


def photo_for(domain: str, company: str, *, transport: LogoTransport | None = None) -> bytes:
    """Fetch + compose. Cached on (domain, company) for the default transport."""
    host = sanitize_hostname(domain)
    name = (company or "").strip()
    if transport is not None:
        return contact_photo(fetch_logo(host, transport=transport), company=name)
    return _cached_photo(host, name)


@lru_cache(maxsize=256)
def _cached_photo(domain: str, company: str) -> bytes:
    return contact_photo(fetch_logo(domain), company=company)


def _host_of(url: str) -> str:
    if not url:
        return ""
    if "://" in url:
        return urlparse(url).hostname or ""
    return url


def _is_ats_or_aggregator(host: str) -> bool:
    host = host.lower().removeprefix("www.")
    for suffix in _ATS_SUFFIXES:
        suffix = suffix.lower().removeprefix("www.")
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def _compose_logo(logo: Image.Image, *, company: str) -> bytes:
    rgb = _dominant_color(logo) or _pastel_from(company)
    canvas = _gradient_canvas(rgb)
    fitted = _fit_logo(logo, int(PHOTO_SIZE * 0.55))
    x = (PHOTO_SIZE - fitted.width) // 2
    y = (PHOTO_SIZE - fitted.height) // 2
    canvas.paste(fitted, (x, y), fitted)
    return _png_bytes(canvas)


def _monogram(company: str) -> bytes:
    rgb = _pastel_from(company)
    canvas = _gradient_canvas(rgb)
    draw = ImageDraw.Draw(canvas)
    initial = _initial(company)
    font = _font(size=220)
    bbox = draw.textbbox((0, 0), initial, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((PHOTO_SIZE - tw) / 2 - bbox[0], (PHOTO_SIZE - th) / 2 - bbox[1]),
        initial,
        fill=(255, 255, 255, 230),
        font=font,
    )
    return _png_bytes(canvas)


def _dominant_color(logo: Image.Image) -> tuple[int, int, int] | None:
    sample = logo.copy()
    sample.thumbnail((32, 32), Image.Resampling.BOX)
    pixels = list(sample.getdata())
    kept: list[tuple[int, int, int]] = []
    for px in pixels:
        r, g, b, a = px if len(px) == 4 else (*px, 255)
        if a < 128:
            continue
        if r >= _NEAR_WHITE and g >= _NEAR_WHITE and b >= _NEAR_WHITE:
            continue
        kept.append((r, g, b))
    if not kept:
        return None
    n = len(kept)
    r = sum(p[0] for p in kept) // n
    g = sum(p[1] for p in kept) // n
    b = sum(p[2] for p in kept) // n
    return _lighten((r, g, b))


def _lighten(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    r, g, b = rgb
    return (
        int(r * 0.45 + 255 * 0.55),
        int(g * 0.45 + 255 * 0.55),
        int(b * 0.45 + 255 * 0.55),
    )


def _pastel_from(company: str) -> tuple[int, int, int]:
    digest = hashlib.sha1((company or "?").encode("utf-8")).digest()
    hue_seed = digest[0]
    # Fixed-saturation pastels so two companies never collide into grey.
    palette = (
        (232, 184, 196),
        (184, 208, 232),
        (196, 220, 184),
        (232, 208, 168),
        (208, 184, 232),
        (168, 216, 216),
        (232, 196, 176),
        (184, 196, 232),
    )
    return _lighten(palette[hue_seed % len(palette)])


def _gradient_canvas(rgb: tuple[int, int, int]) -> Image.Image:
    canvas = Image.new("RGBA", (PHOTO_SIZE, PHOTO_SIZE), (*rgb, 255))
    overlay = Image.new("RGBA", (PHOTO_SIZE, PHOTO_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    darker = tuple(max(0, c - 28) for c in rgb)
    for y in range(PHOTO_SIZE):
        alpha = int(40 * (y / (PHOTO_SIZE - 1)))
        draw.line([(0, y), (PHOTO_SIZE, y)], fill=(*darker, alpha))
    return Image.alpha_composite(canvas, overlay)


def _fit_logo(logo: Image.Image, box: int) -> Image.Image:
    fitted = logo.copy()
    fitted.thumbnail((box, box), Image.Resampling.LANCZOS)
    return fitted


def _initial(company: str) -> str:
    for char in (company or "").strip():
        if char.isalnum():
            return char.upper()
    return "?"


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
