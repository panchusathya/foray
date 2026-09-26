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


def test_served_without_a_token(client):
    response = client.get("/v1/intake/linq/contact-card.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert "immutable" in response.headers.get("cache-control", "")
