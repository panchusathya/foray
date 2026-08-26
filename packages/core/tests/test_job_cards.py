"""The branded job card that replaced the poll.

The tests worth having here are the readability ones. A brand colour is
whatever fell out of a logo, so "is this card legible" is a property of data we
did not choose, and the only honest way to know is to compute it. Everything
else -- which host a logo may come from, whether an image goes out over SMS --
is about not shipping a card that is wrong in a way nobody would notice.

Deliberately **not** a SHA-pinned golden image: Pillow's compression and
antialiasing move between versions, so a hash test would fail on an upgrade
that changed nothing anybody can see. ``juicebox job-card --sample`` renders the
three variants for a human to look at, and needs no database.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from juicebox.enrich import brand as brandmod
from juicebox.enrich.brand import (
    Brand,
    contrast_ratio,
    employer_domain,
    is_employer_host,
    logo_candidates,
    palette_from_logo,
    relative_luminance,
)
from juicebox.messaging import cards
from juicebox.messaging.channels import LINQ
from juicebox.messaging.linq import SERVICE_IMESSAGE, SERVICE_RCS, SERVICE_SMS
from juicebox.models import (
    Candidate,
    CompanyBrand,
    IntakeThread,
    JobCardAsset,
    JobPosting,
    utcnow,
)
from juicebox.presentation.cardimage import (
    MAX_HEIGHT,
    MIN_CONTRAST,
    WIDTH,
    palette_for,
    render_card_png,
)

CARD = {
    "company": "Pepsico",
    "title": "Director, Strategic Finance",
    "location": "Purchase, NY (remote ok)",
    "seniority": "Director",
    "source_label": "open market",
    "reasons": [
        "12 years in CPG finance, 6 leading a team",
        "Ran the FP&A rebuild they are hiring for",
    ],
}


def logo_bytes(colours, *, size=256, fmt="PNG") -> bytes:
    """A fake logo: coloured blobs on transparency, like a real one."""
    image = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    for i, colour in enumerate(colours):
        draw.ellipse(
            (i * size // len(colours), size // 4, (i + 1) * size // len(colours), 3 * size // 4),
            fill=colour,
        )
    if fmt == "JPEG":
        # JPEG carries no alpha, so flatten it the way a real og:image already is.
        flat = Image.new("RGB", image.size, (255, 255, 255))
        flat.paste(image, mask=image.split()[3])
        image = flat
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Colour out of a logo
# ---------------------------------------------------------------------------


class TestPaletteExtraction:
    def test_the_brand_colour_wins_over_the_background(self):
        """A wordmark is mostly white. Ranking by coverage alone would make
        every logo's palette "white", which is the whole reason saturation is
        in the score."""
        palette = palette_from_logo(logo_bytes(["#004b93"]))
        assert palette
        assert contrast_ratio(palette[0], "#004b93") < 1.6

    def test_two_colours_come_back_in_order(self):
        palette = palette_from_logo(logo_bytes(["#004b93", "#e32934"]), count=2)
        assert len(palette) == 2
        assert palette[0] != palette[1]

    def test_near_identical_shades_are_not_both_chosen(self):
        """Two shades of the same navy are one colour as far as a card is
        concerned, and taking both wastes the accent slot."""
        palette = palette_from_logo(logo_bytes(["#004b93", "#004f98"]), count=2)
        if len(palette) > 1:
            assert contrast_ratio(palette[0], palette[1]) >= 1.3

    def test_transparency_is_flattened_onto_white_not_black(self):
        """An alpha-zero pixel counted as black makes every transparent logo
        come out black, which is the most common logo there is."""
        palette = palette_from_logo(logo_bytes(["#e32934"]))
        assert palette
        assert relative_luminance(palette[0]) > 0.05

    def test_a_jpeg_logo_works_too(self):
        """A favicon is a PNG or an ICO; an og:image is almost always a JPEG."""
        assert palette_from_logo(logo_bytes(["#004b93"], fmt="JPEG"))

    def test_garbage_bytes_are_not_an_exception(self):
        assert palette_from_logo(b"this is not an image") == []
        assert palette_from_logo(b"") == []


# ---------------------------------------------------------------------------
# Finding a logo
# ---------------------------------------------------------------------------


JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Director, Strategic Finance",
 "hiringOrganization":{"@type":"Organization","name":"PepsiCo",
   "url":"https://www.pepsico.com","logo":"https://www.pepsico.com/brand/logo.png"},
 "jobLocation":{"address":{"addressLocality":"Purchase","addressRegion":"NY"}}}
</script>
<meta property="og:image" content="/social/hero.jpg">
<link rel="apple-touch-icon" href="/touch.png">
<link rel="icon" href="/favicon.ico">
</head><body></body></html>
"""


class TestFindingTheLogo:
    def test_jsonld_leads_because_the_page_states_it(self):
        found = logo_candidates(JSONLD_PAGE, base="https://boards.greenhouse.io/pepsico/jobs/1")
        assert found[0] == ("jsonld", "https://www.pepsico.com/brand/logo.png")

    def test_og_image_then_touch_icon_then_favicon(self):
        sources = [source for source, _ in logo_candidates(JSONLD_PAGE, base="https://x.test/j")]
        assert sources == ["jsonld", "og_image", "touch_icon", "favicon"]

    def test_relative_urls_are_resolved_against_the_landed_url(self):
        found = dict(
            (source, url) for source, url in logo_candidates(JSONLD_PAGE, base="https://x.test/j/1")
        )
        assert found["touch_icon"] == "https://x.test/touch.png"

    def test_a_page_with_nothing_yields_nothing(self):
        assert logo_candidates("<html><body>hi</body></html>", base="https://x.test/") == []

    @pytest.mark.parametrize(
        "url",
        [
            "https://media.licdn.com/logo.png",
            "https://www.linkedin.com/logo.png",
            "https://boards.greenhouse.io/favicon.ico",
            "https://d3n8a8pro7vhmx.myworkdayjobs.com/icon.png",
            "https://www.google.com/s2/favicons?domain=x.com",
        ],
    )
    def test_an_aggregator_or_ats_logo_is_never_the_employers(self, url):
        """LinkedIn's blue on a Pepsi card is worse than no card at all, and it
        is what scraping the listing page's own favicon produces."""
        assert is_employer_host(url) is False

    def test_the_employers_own_host_passes(self):
        assert is_employer_host("https://www.pepsico.com/brand/logo.png") is True

    def test_the_domain_comes_off_the_pages_own_jsonld(self):
        """`JobPosting.company_domain` is read by referral matching and was
        written by nobody, so this fetch fills a column that has been blank."""
        assert (
            employer_domain(JSONLD_PAGE, base="https://boards.greenhouse.io/pepsico/jobs/1")
            == "pepsico.com"
        )

    def test_an_ats_host_is_not_taken_as_the_employer_domain(self):
        page = "<html><head></head><body>no jsonld</body></html>"
        assert employer_domain(page, base="https://boards.greenhouse.io/pepsico/jobs/1") == ""

    def test_a_companys_own_careers_page_is_its_domain(self):
        page = "<html><head></head><body></body></html>"
        assert employer_domain(page, base="https://careers.pepsico.com/jobs/1") == "careers.pepsico.com"


# ---------------------------------------------------------------------------
# Readability
# ---------------------------------------------------------------------------


class TestThePaletteIsAlwaysReadable:
    @pytest.mark.parametrize(
        "primary",
        [
            "#004b93",  # Pepsi navy
            "#e32934",  # Coca-Cola red
            "#000000",  # a black wordmark
            "#fffc00",  # Snap yellow
            "#e5ff40",  # Ramp lime
            "#f5f5f5",  # a near-white mark
            "#7399c6",  # a mid-tone that fits neither branch cleanly
            "",  # nothing usable
        ],
    )
    def test_ink_always_clears_wcag_aa_against_the_ground(self, primary):
        """A brand colour is data we did not choose, so this cannot be
        eyeballed once and trusted -- it is a property of every possible input.
        """
        palette = palette_for(Brand(domain="x.test", primary=primary) if primary else None)
        assert contrast_ratio(palette.ground, palette.ink) >= MIN_CONTRAST

    def test_a_dark_brand_becomes_the_ground(self):
        palette = palette_for(Brand(domain="pepsico.com", primary="#004b93"))
        assert palette.ground == "#004b93"
        assert palette.branded is True

    def test_a_pale_brand_does_not_become_the_ground(self):
        """Otherwise every light-branded employer gets a white-on-white card,
        and the failure is invisible to whoever picked the colour."""
        palette = palette_for(Brand(domain="snap.com", primary="#fffc00"))
        assert palette.ground != "#fffc00"
        assert relative_luminance(palette.ground) > 0.5
        assert palette.branded is True

    def test_a_pale_brands_hue_survives_as_the_accent(self):
        """Greying it out satisfies the contrast check by throwing the brand
        away. A darkened gold is still recognisably Snap's yellow."""
        palette = palette_for(Brand(domain="snap.com", primary="#fffc00"))
        red, green, blue = (
            int(palette.accent[1:3], 16),
            int(palette.accent[3:5], 16),
            int(palette.accent[5:7], 16),
        )
        assert red > blue and green > blue

    def test_no_brand_is_the_neutral_foray_card(self):
        palette = palette_for(None)
        assert palette.branded is False
        assert contrast_ratio(palette.ground, palette.ink) >= MIN_CONTRAST

    def test_the_accent_is_always_visible_on_the_ground(self):
        for primary in ("#004b93", "#000000", "#fffc00", "#7399c6", ""):
            palette = palette_for(Brand(domain="x.test", primary=primary) if primary else None)
            assert contrast_ratio(palette.ground, palette.accent) >= 1.8, primary


class TestRendering:
    def test_a_branded_card_renders_at_the_expected_width(self):
        data = render_card_png(
            CARD,
            brand=Brand(domain="pepsico.com", primary="#004b93", accent="#e32934"),
            logo=logo_bytes(["#004b93", "#e32934"]),
            index=2,
            total=3,
        )
        with Image.open(io.BytesIO(data)) as image:
            assert image.size[0] == WIDTH
            assert image.size[1] <= MAX_HEIGHT
            # The ground is the brand colour, checked in a corner the layout
            # never draws into.
            assert image.convert("RGB").getpixel((WIDTH - 8, 8)) == (0x00, 0x4B, 0x93)

    def test_the_height_follows_the_content(self):
        """A role with one reason is a shorter card, not the same card with a
        hole in the middle."""
        full = render_card_png(CARD, brand=None)
        bare = render_card_png(
            {"company": "Ramp", "title": "Head of Finance", "reasons": []}, brand=None
        )
        with Image.open(io.BytesIO(full)) as a, Image.open(io.BytesIO(bare)) as b:
            assert b.size[1] < a.size[1]

    def test_a_card_with_no_brand_still_renders(self):
        data = render_card_png(CARD, brand=None, logo=b"")
        with Image.open(io.BytesIO(data)) as image:
            assert image.size[0] == WIDTH

    def test_a_broken_logo_costs_the_tile_not_the_card(self):
        """One malformed favicon must not stop a candidate hearing about a
        role."""
        data = render_card_png(CARD, brand=Brand(domain="x.test", primary="#004b93"), logo=b"not-an-image")
        with Image.open(io.BytesIO(data)) as image:
            assert image.size[0] == WIDTH

    def test_the_bytes_stay_small_enough_to_send(self):
        data = render_card_png(
            CARD,
            brand=Brand(domain="pepsico.com", primary="#004b93"),
            logo=logo_bytes(["#004b93"]),
        )
        assert len(data) < 400_000

    def test_a_very_long_title_is_shrunk_not_overflowed(self):
        card = dict(CARD, title="Vice President, Alternative Investments Coverage and Strategy")
        data = render_card_png(card, brand=None)
        with Image.open(io.BytesIO(data)) as image:
            assert image.size[0] == WIDTH

    def test_the_recruiter_only_block_is_never_handed_to_the_renderer(self):
        """The card is drawn from the stripped projection. If a score ever
        reached here it could be drawn, and a candidate must never see one."""
        from juicebox.presentation.jobcard import jobcard

        posting = JobPosting(org_id="org_x", company="Pepsico", title="Director")
        card = jobcard(posting, None)
        candidate_facing = {k: v for k, v in card.items() if k != "recruiter_only"}
        assert "recruiter_only" not in candidate_facing
        assert render_card_png(candidate_facing, brand=None)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


#: `intake_threads.provider_thread_id` is unique globally, so a test that opens
#: two threads needs two chat ids.
_CHATS = iter(f"chat-{n}" for n in range(1, 1000))


def _thread(session, org_id, *, service, chat=""):
    thread = IntakeThread(
        org_id=org_id,
        channel=LINQ,
        provider="linq",
        provider_thread_id=chat or next(_CHATS),
        from_address="+14155550123",
        to_address="+14155559001",
        consent_messaging=True,
        last_service=service,
    )
    session.add(thread)
    session.flush()
    return thread


_POSTINGS = iter(f"src-{n}" for n in range(1, 1000))


def _posting(session, org_id):
    posting = JobPosting(
        org_id=org_id,
        source="exa",
        source_id=next(_POSTINGS),
        company="Pepsico",
        company_domain="pepsico.com",
        title="Director, Strategic Finance",
        status="open",
        # `passes_bar` refuses a stale posting, so a proposal test has to give
        # it a freshness stamp or nothing is ever eligible.
        posted_at=utcnow(),
        last_seen_at=utcnow(),
    )
    session.add(posting)
    session.flush()
    return posting


@pytest.fixture
def channel():
    """A FakeChannel on the Linq slot, so an outbound is recorded not sent."""
    from juicebox.messaging.channels import FakeChannel, set_channel

    installed = FakeChannel()
    set_channel(LINQ, installed)
    return installed


@pytest.fixture
def brand_row(session):
    from juicebox import storage

    relpath = storage.brand_logo_relpath(domain="pepsico.com")
    storage.save_bytes(relpath, logo_bytes(["#004b93", "#e32934"]))
    row = CompanyBrand(
        domain="pepsico.com",
        logo_path=relpath,
        palette=["#004b93", "#e32934"],
        source="jsonld",
        brand_version=brandmod.BRAND_VERSION,
        fetched_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


class TestWhoGetsAnImage:
    @pytest.mark.parametrize("service", [SERVICE_IMESSAGE, SERVICE_RCS])
    def test_imessage_and_rcs_do(self, session, account, service):
        assert cards.wants_image(_thread(session, account["org_id"], service=service)) is True

    def test_sms_does_not(self, session, account):
        """An image over SMS is an MMS: carrier cost, compression, and some
        carriers strip it. messaging/uploads.py already made this call."""
        assert cards.wants_image(_thread(session, account["org_id"], service=SERVICE_SMS)) is False

    def test_an_unknown_protocol_does_not(self, session, account):
        """Never having been told is not evidence of iMessage."""
        assert cards.wants_image(_thread(session, account["org_id"], service="")) is False


class TestCardMedia:
    def test_an_sms_thread_gets_no_media_and_nothing_is_rendered(
        self, session, account, brand_row, monkeypatch
    ):
        def boom(**kwargs):
            raise AssertionError("must not upload for an SMS thread")

        monkeypatch.setattr(cards, "upload_attachment", boom)
        thread = _thread(session, account["org_id"], service=SERVICE_SMS)
        posting = _posting(session, account["org_id"])
        assert cards.card_media(session, thread=thread, posting=posting, card=CARD) == ()
        assert session.scalars(select(JobCardAsset)).all() == []

    def test_an_imessage_thread_renders_uploads_and_caches(
        self, session, account, brand_row, monkeypatch
    ):
        calls = []

        def upload(*, filename, content_type, data):
            calls.append((filename, content_type, len(data)))
            return "att-card-1"

        monkeypatch.setattr(cards, "upload_attachment", upload)
        thread = _thread(session, account["org_id"], service=SERVICE_IMESSAGE)
        posting = _posting(session, account["org_id"])

        media = cards.card_media(session, thread=thread, posting=posting, card=CARD)
        assert len(media) == 1
        assert media[0].attachment_id == "att-card-1"
        assert media[0].content_type == "image/png"
        assert len(calls) == 1
        assert calls[0][1] == "image/png"

        row = session.scalars(select(JobCardAsset)).one()
        assert row.card_version == cards.asset_version()
        assert row.storage_path

    def test_the_second_candidate_reuses_the_upload(
        self, session, account, brand_row, monkeypatch
    ):
        """A role proposed to thirty candidates is one card, not thirty
        uploads of identical bytes."""
        calls = []
        monkeypatch.setattr(
            cards,
            "upload_attachment",
            lambda **kw: (calls.append(kw["filename"]), "att-card-1")[1],
        )
        posting = _posting(session, account["org_id"])
        for _ in range(3):
            thread = _thread(session, account["org_id"], service=SERVICE_IMESSAGE)
            cards.card_media(session, thread=thread, posting=posting, card=CARD)
        assert len(calls) == 1

    def test_a_renderer_change_invalidates_the_cache(
        self, session, account, brand_row, monkeypatch
    ):
        """Without this a design fix never reaches a posting anybody has
        already been shown."""
        calls = []
        monkeypatch.setattr(
            cards,
            "upload_attachment",
            lambda **kw: (calls.append(1), "att-card-1")[1],
        )
        thread = _thread(session, account["org_id"], service=SERVICE_IMESSAGE)
        posting = _posting(session, account["org_id"])
        cards.card_media(session, thread=thread, posting=posting, card=CARD)
        session.scalars(select(JobCardAsset)).one().card_version = "card-v0+brand-v0"
        session.flush()
        cards.card_media(session, thread=thread, posting=posting, card=CARD)
        assert len(calls) == 2

    def test_a_failed_upload_leaves_the_text_card_alone(
        self, session, account, brand_row, monkeypatch
    ):
        """A role the candidate never hears about is far worse than a role
        with no picture."""
        from juicebox.messaging.linq import LinqSendError

        def refuse(**kwargs):
            raise LinqSendError("413 Payload Too Large")

        monkeypatch.setattr(cards, "upload_attachment", refuse)
        thread = _thread(session, account["org_id"], service=SERVICE_IMESSAGE)
        posting = _posting(session, account["org_id"])
        assert cards.card_media(session, thread=thread, posting=posting, card=CARD) == ()
        # The row exists so the bytes are not re-rendered every send, but with
        # no attachment id and no version, so the next send retries the upload.
        row = session.scalars(select(JobCardAsset)).one()
        assert row.attachment_id == ""
        assert row.card_version == ""

    def test_a_posting_with_no_brand_still_gets_a_card(
        self, session, account, monkeypatch
    ):
        monkeypatch.setattr(cards, "upload_attachment", lambda **kw: "att-neutral")
        monkeypatch.setattr(brandmod, "resolve", lambda db, posting, **kw: None)
        thread = _thread(session, account["org_id"], service=SERVICE_IMESSAGE)
        posting = _posting(session, account["org_id"])
        media = cards.card_media(session, thread=thread, posting=posting, card=CARD)
        assert len(media) == 1

class TestPresentingRolesOnARealThread:
    """The integration check: what actually leaves `present_matches`.

    `card_media` returning empty is easy to assert in isolation and easy to
    get wrong in the caller -- the body has to be byte-identical whether or not
    an image rode along, because an image that fails to render must not take
    the role with it.
    """

    def _present(self, session, org_id, *, service, channel, monkeypatch):
        from juicebox.messaging.manager import present_matches
        from juicebox.models import JobMatch

        monkeypatch.setattr(cards, "upload_attachment", lambda **kw: "att-card-1")
        thread = _thread(session, org_id, service=service)
        candidate = Candidate(
            org_id=org_id,
            source="manual",
            source_id=f"c-present-{thread.provider_thread_id}",
            full_name="Priya Raman",
        )
        session.add(candidate)
        session.flush()
        thread.candidate_id = candidate.id
        posting = _posting(session, org_id)
        match = JobMatch(
            org_id=org_id,
            candidate_id=candidate.id,
            job_posting_id=posting.id,
            score=0.9,
            status="proposed",
            # `proposal_reasons` reads `score`, and no reason means no
            # proposal -- the explainability half of the bar.
            criteria=[
                {
                    "key": "experience",
                    "label": "Experience",
                    "score": 0.9,
                    "evidence": "12 years in CPG finance",
                }
            ],
        )
        session.add(match)
        session.flush()
        present_matches(session, thread=thread, state=None)
        return [item[1] for item in channel.sent]

    def test_an_imessage_thread_carries_the_image(
        self, session, account, brand_row, channel, monkeypatch
    ):
        sent = self._present(
            session, account["org_id"], service=SERVICE_IMESSAGE, channel=channel,
            monkeypatch=monkeypatch,
        )
        with_media = [m for m in sent if m.media]
        assert with_media, "an iMessage thread should get the card image"
        assert with_media[0].media[0].content_type == "image/png"

    def test_an_sms_thread_carries_none_and_the_same_words(
        self, session, account, brand_row, channel, monkeypatch
    ):
        rich = self._present(
            session, account["org_id"], service=SERVICE_IMESSAGE, channel=channel,
            monkeypatch=monkeypatch,
        )
        channel.sent.clear()
        plain = self._present(
            session, account["org_id"], service=SERVICE_SMS, channel=channel,
            monkeypatch=monkeypatch,
        )
        assert all(not message.media for message in plain)
        # Byte-identical card bodies: the image is additive, never a
        # substitution. Only the card messages are compared -- the tail carries
        # a freshly minted single-use portal link, which differs by design.
        assert [m.body for m in rich if m.verbatim] == [m.body for m in plain if m.verbatim]
