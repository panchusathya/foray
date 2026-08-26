"""A card's colours, taken from the employer's own logo.

No vendor and no key, and that is not a compromise -- **the logo already
contains the brand**. Clearbit's free logo API is gone, Brandfetch and logo.dev
both want a key, and each would answer for fewer employers than the mark we
already fetch: ``presentation.logo`` pulls a favicon for the posting's domain
today to paint a contact card, and that bitmap is the brand.

So the colours are quantised out of it. Ranked by saturation weighted by
coverage, with near-white and near-black dropped: a wordmark is mostly
background, and the most *common* pixel in a logo is almost never the brand
colour.

**This module holds no I/O and no state.** It is pure colour maths over bytes
somebody else fetched, which is what lets ``cardimage`` assert contrast rather
than eyeball it, and what keeps a palette miss from ever being a failed send.
A richer version of this once came with a ``CompanyBrand`` table and a crawl of
the employer's own posting page for a ``schema.org`` logo; that buys a better
palette for a minority of employers and is a separate change. Nothing here
needs a session, a migration, or a network call.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Bumped when the extraction rules change, so a card cached under the old
#: palette is re-rendered rather than serving colours these rules would not
#: have chosen. Read by ``messaging.cards.asset_version``.
PALETTE_VERSION = "palette-v1"


@dataclass(frozen=True)
class Brand:
    """The two colours a card can use. Never the bitmap -- that is passed
    separately, because the renderer draws the logo and only *reads* these."""

    #: Only for logs: which employer these came out of.
    domain: str = ""
    #: ``#rrggbb``. Empty when no usable colour came out of the logo, which is
    #: an ordinary outcome -- a monochrome favicon has no brand colour in it.
    primary: str = ""
    accent: str = ""

    def __bool__(self) -> bool:
        return bool(self.primary)


def brand_from_logo(data: bytes, *, domain: str = "") -> Brand:
    """The employer's colours, or an empty ``Brand`` when the logo has none.

    Empty is not a failure and the caller must not treat it as one:
    ``cardimage`` falls back to the neutral ground, which is our own colour, so
    a logo with nothing extractable still produces a card that looks deliberate.
    """
    colours = palette_from_logo(data, count=2) if data else []
    return Brand(
        domain=domain,
        primary=colours[0] if colours else "",
        accent=colours[1] if len(colours) > 1 else "",
    )


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
