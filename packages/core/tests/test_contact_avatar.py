"""The contact photo, and the one route that serves it.

Thin on purpose: the picture is four squares on a ground, so what is worth
pinning is that it renders at all, that it is square (Apple crops it to a
circle and a non-square source crops off-centre), and that the route is
reachable without a token -- Linq fetches it as an anonymous GET, and an
auth-guarded photo is a card with no picture and no error anybody sees.
"""

from __future__ import annotations

import io

from juicebox.presentation.contactavatar import render_avatar_png


def test_renders_a_square_png():
    from PIL import Image

    image = Image.open(io.BytesIO(render_avatar_png(256)))
    assert image.format == "PNG"
    assert image.size == (256, 256)


def test_ground_is_drawn_not_transparent():
    """A transparent avatar is composited by whoever displays it."""
    from PIL import Image

    image = Image.open(io.BytesIO(render_avatar_png(256)))
    assert image.mode == "RGB"
    assert image.getpixel((4, 4)) == (10, 10, 17)


def test_mark_is_inside_the_circle_apple_will_crop():
    """The corners have to be empty ground; the middle has to be ink."""
    from PIL import Image

    image = Image.open(io.BytesIO(render_avatar_png(256)))
    assert image.getpixel((250, 250)) == (10, 10, 17)
    # Middle of the top-left square: 6/32 + half of 8.5/32, at 256px.
    assert image.getpixel((82, 82)) == (125, 123, 245)


def test_served_without_a_token(client):
    response = client.get("/v1/intake/linq/contact-card.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert "immutable" in response.headers.get("cache-control", "")
