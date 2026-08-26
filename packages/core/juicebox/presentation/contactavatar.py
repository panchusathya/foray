"""The Foray mark as a contact photo.

Apple crops a contact photo to a circle and draws it at about forty pixels,
which decides everything in here:

* **The mark, not the lockup.** ``foray-lockup-on-light.svg`` is 190x30 — a
  circular crop of it keeps roughly the first square and none of the word. The
  mark is square and survives the crop, so the mark is what a stranger sees.
* **On a ground, not transparent.** A transparent PNG is composited by
  whatever is behind it, which in a contact sheet is a colour we do not
  choose. ``foray-icon-tile-indigo.svg`` is the house answer to exactly this
  question — the mark inset on ``#0A0A11`` — so this is that tile, redrawn at
  photo resolution.
* **Drawn, not rasterised.** Four rounded squares is less code than an SVG
  rasteriser is a dependency, and it keeps the geometry legible next to the
  source file it copies.

Geometry follows ``foray-icon-tile-indigo.svg``, with one change. That file is
an app-icon tile, which iOS does not crop, so it insets the mark to 44% of the
edge; inside a circle that reads as a small mark adrift on a large ground.
Here the mark is scaled to span 62% — comfortably inside the circle Apple
draws, and filling it the way an avatar should. The square-to-gap ratio is
kept, so it is the same mark at a different size and not a redrawn one.
"""

from __future__ import annotations

import io
from functools import lru_cache

#: The tile, in the source SVG's 32-unit grid.
TILE = 32
GROUND = (10, 10, 17)  # #0A0A11
INK = (125, 123, 245)  # #7D7BF5
SQUARE = 8.5
RADIUS = 2.0
#: Top-left corner of each square, with the opacity the brand gives it. The
#: pair spans 6..26 of 32 — centred, and 62% of the edge.
SQUARES = (
    (6, 6, 1.0),
    (17.5, 6, 0.45),
    (6, 17.5, 0.35),
    (17.5, 17.5, 0.8),
)

CONTENT_TYPE = "image/png"
#: Bumped when the pixels change, so a card set from an older one can be told
#: apart from a card set from this one.
AVATAR_VERSION = "1"


@lru_cache(maxsize=4)
def render_avatar_png(size: int = 1024) -> bytes:
    """The contact photo, as PNG bytes. Deterministic, so it is cached.

    ``size`` is the square edge in pixels. 1024 is what the route serves:
    large enough that Apple never upscales it, small enough to stay a few
    kilobytes of flat colour.
    """
    from PIL import Image, ImageDraw

    scale = size / TILE
    image = Image.new("RGBA", (size, size), (*GROUND, 255))
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for x, y, opacity in SQUARES:
        left, top = x * scale, y * scale
        draw.rounded_rectangle(
            (left, top, left + SQUARE * scale, top + SQUARE * scale),
            radius=RADIUS * scale,
            fill=(*INK, round(255 * opacity)),
        )
    buffer = io.BytesIO()
    Image.alpha_composite(image, overlay).convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()
