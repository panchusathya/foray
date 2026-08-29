"""The job card as an image — the fourth renderer over one projection.

``jobcard.py`` computes the card; ``render_card`` and ``render_text`` are the
text renderers and this is the picture. It reads the same dict and computes
nothing of its own, which is the rule that whole package exists to hold.

**Why an image at all.** A role in a text bubble is a wall the eye slides off,
and the poll this replaces read as a survey rather than a job. A card carrying
the employer's own logo on the employer's own colour reads, in one glance, as
that company's posting — which is what it is.

Three things in here are deliberate and easy to undo by accident:

- **Contrast is computed, never eyeballed.** A brand colour is whatever came
  out of a logo, so the ink is chosen against the ground's luminance and the
  result is asserted to clear WCAG AA. A card that fails falls back to the
  neutral variant rather than shipping a role nobody can read.
- **A pale brand does not become the ground.** A yellow or near-white primary
  becomes a rule and an accent instead. Otherwise every light-branded employer
  gets an illegible card, and the failure is invisible to whoever picked the
  colour.
- **The logo sits in a white tile.** An arbitrary logo on an arbitrary
  background is a coin flip; a tile makes it legible every time, and it is what
  every job board does for the same reason.
- **The height is measured, not fixed.** A role with one reason is a shorter
  card, not the same card with a hole in it. iMessage scales an image to the
  bubble width regardless, so a fixed canvas only ever shows as dead space.

The card never draws a score. It takes the already-stripped projection, the
same dict ``manager.present_matches`` hands to ``render_card``.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .palette import Brand, contrast_ratio, relative_luminance, rgb_of

logger = logging.getLogger(__name__)

#: Bumped on any change that alters the pixels, so cached cards are re-rendered
#: instead of a design fix never reaching a posting somebody has already seen.
CARD_VERSION = "card-v1"

WIDTH = 1200
#: Height is computed from the content, so this is a ceiling for tests to
#: assert against rather than the canvas size.
MAX_HEIGHT = 720
PAD = 56

_FONT_DIR = Path(__file__).parent / "fonts"
_REGULAR = _FONT_DIR / "Inter-Regular.ttf"
_SEMIBOLD = _FONT_DIR / "Inter-SemiBold.ttf"

#: The neutral ground, used when there is no brand and when a brand colour
#: cannot be made readable. Foray's own dark green (packages/portal/styles.css
#: `--brand`), so a card with no logo still looks like it came from us.
NEUTRAL_GROUND = "#12332c"
NEUTRAL_ACCENT = "#4cc38a"
LIGHT_GROUND = "#f5f5f3"

INK_LIGHT = "#ffffff"
INK_DARK = "#17211e"

#: Above this luminance a colour cannot be a ground -- it becomes the accent.
MAX_GROUND_LUMINANCE = 0.62
#: WCAG AA for **normal** text, which is the bar this needs even though the
#: title is 56px: the same ink also draws the reasons and the footer, and those
#: are body sizes. Do not drop this to 3.0 on the grounds that AA-large allows
#: it -- 3.0 is the large-text threshold and it does not cover most of the
#: words on the card. Anything that fails here is not rejected, it degrades to
#: the light-ground variant below, so the cost of the stricter bar is a
#: differently-coloured card rather than a lost brand.
MIN_CONTRAST = 4.5
#: An accent is a square and a hairline, not text. It has to be visible, not
#: readable, so the bar is far below AA.
MIN_ACCENT_CONTRAST = 1.8

MAX_TITLE_CHARS = 64
MAX_COMPANY_CHARS = 34
MAX_META_CHARS = 58
MAX_REASON_CHARS = 62
MAX_REASONS = 2


@dataclass(frozen=True)
class Palette:
    """The resolved colours for one card, after the readability rules."""

    ground: str
    ink: str
    muted: str
    accent: str
    #: True when the brand's own colour is on the card at all. False means the
    #: neutral variant -- worth knowing, because it is the honest answer to
    #: "did branding work for this employer".
    branded: bool = False


def _mix(one: str, two: str, weight: float) -> str:
    """``weight`` of ``two`` over ``one``. Used for muted ink and hairlines,
    because a real blend against the ground beats an alpha the PNG cannot
    carry over an opaque background anyway."""
    a, b = _rgb_tuple(one), _rgb_tuple(two)
    return "#{:02x}{:02x}{:02x}".format(
        *(round(x + (y - x) * weight) for x, y in zip(a, b, strict=True))
    )


def palette_for(brand: Brand | None) -> Palette:
    """Ground, ink and accent for this employer — readable or neutral.

    The ladder: use the brand colour as the ground when it is dark enough to
    take light ink; keep it as an accent when it is not; fall back to Foray's
    own colours when there is nothing usable. Every branch ends in a pairing
    that clears ``MIN_CONTRAST``, and the last branch is why that is provable
    rather than hoped for.
    """
    primary = (brand.primary if brand else "") or ""
    accent = (brand.accent if brand else "") or ""

    if primary and relative_luminance(primary) <= MAX_GROUND_LUMINANCE:
        ground = primary
        ink = INK_LIGHT if relative_luminance(ground) < 0.4 else INK_DARK
        if contrast_ratio(ground, ink) >= MIN_CONTRAST:
            return Palette(
                ground=ground,
                ink=ink,
                muted=_mix(ground, ink, 0.72),
                accent=_readable_accent(ground, accent or primary, ink),
                branded=True,
            )

    if primary:
        # Too pale to sit behind text, but still the company's colour. It goes
        # on a light ground as the accent, which is the other way a job board
        # uses a brand and keeps a yellow logo from producing a white card.
        ground = LIGHT_GROUND
        if contrast_ratio(ground, INK_DARK) >= MIN_CONTRAST:
            return Palette(
                ground=ground,
                ink=INK_DARK,
                muted=_mix(ground, INK_DARK, 0.66),
                accent=_readable_accent(ground, primary, INK_DARK),
                branded=True,
            )

    return Palette(
        ground=NEUTRAL_GROUND,
        ink=INK_LIGHT,
        muted=_mix(NEUTRAL_GROUND, INK_LIGHT, 0.70),
        accent=NEUTRAL_ACCENT,
        branded=False,
    )


def _readable_accent(ground: str, wanted: str, ink: str) -> str:
    """The accent, pushed toward the ink until it is visible on the ground.

    An accent is a small square and a hairline, so it does not need AA — but it
    does need to be *seen*, and a navy accent on a navy ground is a decoration
    nobody can find. The hue is preserved rather than swapped for grey: Snap's
    yellow on a near-white card should read as a dark gold, which is still
    recognisably theirs, and greying it out throws the brand away to satisfy a
    contrast check.
    """
    if not wanted:
        return _mix(ground, ink, 0.55)
    shade = wanted
    for weight in (0.0, 0.2, 0.35, 0.5, 0.65, 0.8):
        shade = _mix(wanted, ink, weight)
        if contrast_ratio(ground, shade) >= MIN_ACCENT_CONTRAST:
            return shade
    return shade


def _font(path: Path, size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        # Vendored and declared as package-data, so this is a broken install
        # rather than a missing optional. Loud in the log, still renders.
        logger.warning("card font %s is missing; falling back to the bitmap font", path)
        return ImageFont.load_default()


def _fit(draw, text: str, font_path: Path, *, size: int, min_size: int, width: int):
    """The largest size at or under ``size`` that fits ``width`` on one line."""
    from PIL import ImageFont

    current = size
    while current > min_size:
        font = _font(font_path, current)
        if draw.textlength(text, font=font) <= width:
            return font
        current -= 2
    return _font(font_path, min_size) if min_size else ImageFont.load_default()


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_card_png(
    card: dict[str, Any],
    *,
    brand: Brand | None = None,
    logo: bytes = b"",
    index: int = 1,
    total: int = 1,
) -> bytes:
    """One role, as a PNG. Never raises — a card is not worth a failed turn.

    The canvas height is computed from the content rather than fixed. A role
    with one reason and no location is a shorter card, not the same card with a
    hole in the middle, and iMessage scales an image to the bubble width either
    way -- so a fixed height buys nothing and shows as dead space.
    """
    from PIL import Image, ImageDraw

    palette = palette_for(brand)
    # A throwaway surface to measure against: `textlength` needs a draw
    # context, and the canvas cannot be allocated until the text is measured.
    ruler = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    company = _clip(card.get("company") or "", MAX_COMPANY_CHARS)
    title = _clip(card.get("title") or "Role", MAX_TITLE_CHARS)
    label = _spaced(_clip((card.get("source_label") or "").upper(), 24))
    meta = _clip(
        " · ".join(
            part for part in (card.get("location") or "", card.get("seniority") or "") if part
        ),
        MAX_META_CHARS,
    )
    reasons = [_clip(r, MAX_REASON_CHARS) for r in (card.get("reasons") or [])[:MAX_REASONS]]

    tile = 96
    text_left = PAD + tile + 28 if logo else PAD
    company_font = _fit(
        ruler, company, _SEMIBOLD, size=32, min_size=22, width=WIDTH - text_left - PAD - 200
    )
    title_font = _fit(ruler, title, _SEMIBOLD, size=58, min_size=32, width=WIDTH - 2 * PAD)
    meta_font = _font(_REGULAR, 25)
    reason_font = _font(_REGULAR, 23)
    footer_font = _font(_REGULAR, 20)

    body_top = PAD + tile + 40
    content_bottom = body_top + title_font.size + 20
    if meta:
        content_bottom += 44
    if reasons:
        content_bottom += 14 + 40 * len(reasons)

    rule_y = content_bottom + 40
    height = rule_y + 20 + footer_font.size * 2 + PAD

    image = Image.new("RGB", (WIDTH, height), palette.ground)
    draw = ImageDraw.Draw(image)

    # --- identity row -----------------------------------------------------
    if logo:
        _paste_logo(image, logo, box=(PAD, PAD, tile), edge=_rgb_tuple(palette.muted))
    draw.text((text_left, PAD + 8), company, font=company_font, fill=palette.ink)
    if label:
        draw.text((text_left, PAD + 52), label, font=_font(_REGULAR, 15), fill=palette.muted)

    # --- the role ---------------------------------------------------------
    y = body_top
    draw.text((PAD, y), title, font=title_font, fill=palette.ink)
    y += title_font.size + 20
    if meta:
        draw.text((PAD, y), meta, font=meta_font, fill=palette.muted)
        y += 44

    # --- why -------------------------------------------------------------
    if reasons:
        y += 14
        for reason in reasons:
            draw.rectangle((PAD, y + 8, PAD + 10, y + 18), fill=palette.accent)
            draw.text((PAD + 26, y), reason, font=reason_font, fill=palette.ink)
            y += 40

    # --- footer ----------------------------------------------------------
    draw.rectangle((PAD, rule_y, WIDTH - PAD, rule_y + 1), fill=palette.accent)
    position = f"{index} of {total}" if total > 1 else "open role"
    draw.text((PAD, rule_y + 20), position, font=footer_font, fill=palette.muted)
    # Persistent, not decorative. The card carries somebody else's logo and
    # colours, and this is the line that keeps it from reading as a message
    # *from* that employer.
    via = "via Foray"
    draw.text(
        (WIDTH - PAD - draw.textlength(via, font=footer_font), rule_y + 20),
        via,
        font=footer_font,
        fill=palette.muted,
    )
    apply = f'reply "apply {index}"'
    draw.text(
        ((WIDTH - draw.textlength(apply, font=footer_font)) / 2, rule_y + 20 + footer_font.size),
        apply,
        font=footer_font,
        fill=palette.muted,
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _rgb_tuple(colour: str) -> tuple[int, int, int]:
    """Black for anything unparseable, so a bad colour is drawn rather than
    raised: a card is never worth a failed turn."""
    return rgb_of(colour) or (0, 0, 0)


def _spaced(text: str) -> str:
    """Letter-spaced caps, since Pillow has no tracking."""
    return " ".join(text)


def _paste_logo(
    image, data: bytes, *, box: tuple[int, int, int], edge: tuple[int, int, int]
) -> None:
    """The logo, contained in a white rounded tile.

    Failures are swallowed: a logo we cannot decode costs the tile, and a card
    without one is still a card. Raising here would mean one malformed favicon
    stops a candidate hearing about a role.
    """
    from PIL import Image, ImageDraw, UnidentifiedImageError

    left, top, size = box
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=16, fill=255)
    # An outline, because a white tile on a near-white ground is invisible --
    # which is exactly the case a pale brand colour produces.
    ImageDraw.Draw(tile).rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=16,
        fill=(255, 255, 255, 255),
        outline=edge,
        width=1,
    )

    try:
        with Image.open(io.BytesIO(data)) as raw:
            logo = raw.convert("RGBA")
            inner = size - 24
            logo.thumbnail((inner, inner))
            tile.paste(
                logo,
                ((size - logo.width) // 2, (size - logo.height) // 2),
                logo,
            )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.info("could not draw the logo on a card: %s", exc)

    image.paste(tile, (left, top), mask)
