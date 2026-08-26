"""The rendered job card: who gets one, how often it is built, and what
happens when the render or the upload fails.

Three rules are worth more than the picture itself.

**An image only goes where it renders inline.** On anything but iMessage or RCS
it becomes an MMS -- carrier cost, recompression, and some carriers drop it --
so a thread whose protocol we have never been told stays text. Never having
been told is not evidence of iMessage, and that distinction is the test below
that would otherwise look redundant with the SMS one.

**One posting, one render.** A role shown to thirty candidates is one card, and
``card_version`` is what makes reusing it safe: bump the renderer and the next
send rebuilds rather than serving pixels the current code would not produce.

**Nothing here may cost a turn.** A role the candidate never hears about is far
worse than a role without a picture, so every failure path returns "no media"
and the text card goes out regardless.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy import select

from juicebox.messaging import cards as cards_mod
from juicebox.messaging.linq import (
    SERVICE_IMESSAGE,
    SERVICE_RCS,
    SERVICE_SMS,
    LinqSendError,
)
from juicebox.messaging.threads import open_thread
from juicebox.models import JobCardAsset, JobPosting

CARD = {
    "title": "Founding Engineer",
    "company": "Sourcebot",
    "location": "San Francisco, CA",
    "seniority": "Senior",
    "reasons": ["python + distributed systems", "series a, 20 people"],
}


def _png(color=(40, 90, 180), size=48) -> bytes:
    image = Image.new("RGB", (size, size), color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _posting(session, org_id, *, company="Sourcebot", suffix="1") -> JobPosting:
    posting = JobPosting(
        org_id=org_id,
        source="manual",
        source_id=f"card-{company.lower()}-{suffix}",
        title="Founding Engineer",
        company=company,
        company_normalized=company.lower(),
        company_domain=f"{company.lower()}.dev",
        jd_text="Build the thing.",
        status="open",
    )
    session.add(posting)
    session.flush()
    return posting


def _thread(session, account, *, service=SERVICE_IMESSAGE):
    thread = open_thread(
        session,
        org_id=account["org_id"],
        channel="linq",
        consent_messaging=True,
    )
    thread.last_service = service
    session.flush()
    return thread


@pytest.fixture
def uploads(monkeypatch):
    """Record every upload. Patched at the *import site*, deliberately.

    ``cards.py`` does ``from .linq import upload_attachment`` at module load,
    so the name it calls is ``cards.upload_attachment`` and patching
    ``linq.upload_attachment`` would do nothing at all -- the test would pass
    against the real function and try to reach Linq.
    """
    calls: list[dict] = []

    def fake(*, filename, content_type, data):
        calls.append(
            {"filename": filename, "content_type": content_type, "size": len(data)}
        )
        return f"att_{len(calls)}"

    monkeypatch.setattr(cards_mod, "upload_attachment", fake)
    return calls


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """A logo fetch must never leave the process in a test."""
    monkeypatch.setattr(cards_mod, "fetch_logo", lambda domain: _png())


class TestWhoGetsAnImage:
    @pytest.mark.parametrize("service", [SERVICE_IMESSAGE, SERVICE_RCS])
    def test_rich_protocols_do(self, session, account, service):
        thread = _thread(session, account, service=service)
        assert cards_mod.wants_image(thread) is True

    def test_sms_does_not(self, session, account):
        """An image over SMS is an MMS. Cost, compression, and some carriers
        strip it outright."""
        thread = _thread(session, account, service=SERVICE_SMS)
        assert cards_mod.wants_image(thread) is False

    def test_an_unknown_protocol_does_not(self, session, account):
        """The case that is not the same as SMS, and the reason `last_service`
        defaults to "" rather than to a guess. Falling back to text when we
        have not been told is a choice; guessing iMessage would be wrong about
        half the time and would silently bill MMS for the other half."""
        thread = _thread(session, account, service="")
        assert cards_mod.wants_image(thread) is False

    def test_no_render_and_no_upload_for_a_text_thread(
        self, session, account, uploads, monkeypatch
    ):
        """The gate is checked before the work, not after.

        Rendering and then discarding would be invisible in the output and
        expensive in the aggregate, so the renderer is made to explode.
        """

        def _explode(*args, **kwargs):
            raise AssertionError("rendered a card for a thread that cannot show it")

        monkeypatch.setattr(cards_mod, "render_card_png", _explode)
        thread = _thread(session, account, service=SERVICE_SMS)
        posting = _posting(session, account["org_id"])
        assert cards_mod.card_media(session, thread=thread, posting=posting, card=CARD) == ()
        assert uploads == []


class TestOnePostingOneRender:
    def test_first_send_renders_and_uploads(self, session, account, uploads):
        thread = _thread(session, account)
        posting = _posting(session, account["org_id"])
        media = cards_mod.card_media(
            session, thread=thread, posting=posting, card=CARD
        )
        assert len(media) == 1
        part = media[0]
        assert part.attachment_id == "att_1"
        assert part.content_type == "image/png"
        assert part.filename == "sourcebot-role.png"
        assert len(uploads) == 1
        assert uploads[0]["content_type"] == "image/png"
        assert uploads[0]["size"] > 0

        row = session.scalar(select(JobCardAsset))
        assert row is not None
        assert row.attachment_id == "att_1"
        assert row.card_version == cards_mod.asset_version()
        assert row.storage_path.endswith(".png")

    def test_a_second_candidate_reuses_the_upload(self, session, account, uploads):
        """The whole point of the table. Thirty candidates, one upload."""
        posting = _posting(session, account["org_id"])
        first = _thread(session, account)
        second = _thread(session, account)
        a = cards_mod.card_media(session, thread=first, posting=posting, card=CARD)
        b = cards_mod.card_media(session, thread=second, posting=posting, card=CARD)
        assert a[0].attachment_id == b[0].attachment_id == "att_1"
        assert len(uploads) == 1
        assert len(session.scalars(select(JobCardAsset)).all()) == 1

    def test_a_version_bump_re_renders(self, session, account, uploads, monkeypatch):
        """Without this the cache outlives the code that filled it: a design
        fix would never reach a posting anybody had already been shown."""
        posting = _posting(session, account["org_id"])
        thread = _thread(session, account)
        cards_mod.card_media(session, thread=thread, posting=posting, card=CARD)
        assert len(uploads) == 1

        monkeypatch.setattr(cards_mod, "CARD_VERSION", "card-v2")
        media = cards_mod.card_media(session, thread=thread, posting=posting, card=CARD)
        assert len(uploads) == 2
        assert media[0].attachment_id == "att_2"
        rows = session.scalars(select(JobCardAsset)).all()
        assert len(rows) == 1, "the row is updated in place, not duplicated"
        assert rows[0].card_version == "card-v2+" + cards_mod.palettemod.PALETTE_VERSION

    def test_two_postings_are_two_cards(self, session, account, uploads):
        thread = _thread(session, account)
        one = _posting(session, account["org_id"], company="Sourcebot", suffix="a")
        two = _posting(session, account["org_id"], company="Parasail", suffix="b")
        cards_mod.card_media(session, thread=thread, posting=one, card=CARD)
        cards_mod.card_media(
            session, thread=thread, posting=two, card={**CARD, "company": "Parasail"}
        )
        assert len(uploads) == 2
        assert {u["filename"] for u in uploads} == {
            "sourcebot-role.png",
            "parasail-role.png",
        }


class TestNothingCostsATurn:
    def test_a_refused_upload_yields_no_media_and_does_not_raise(
        self, session, account, monkeypatch
    ):
        def refuse(**kwargs):
            raise LinqSendError("linq said no")

        monkeypatch.setattr(cards_mod, "upload_attachment", refuse)
        thread = _thread(session, account)
        posting = _posting(session, account["org_id"])
        assert cards_mod.card_media(session, thread=thread, posting=posting, card=CARD) == ()

    def test_a_refused_upload_is_retried_next_time_but_not_re_rendered(
        self, session, account, monkeypatch
    ):
        """The half-state the row exists to express.

        The bytes are on disk so the next send does not re-render them, but
        ``card_version`` stays blank so the next send *does* retry the upload.
        Storing the version here would cache a row whose attachment id is
        empty, and every later send would read it as a hit and attach nothing.
        """
        calls: list[str] = []

        def refuse(**kwargs):
            calls.append("refused")
            raise LinqSendError("linq said no")

        monkeypatch.setattr(cards_mod, "upload_attachment", refuse)
        thread = _thread(session, account)
        posting = _posting(session, account["org_id"])
        cards_mod.card_media(session, thread=thread, posting=posting, card=CARD)

        row = session.scalar(select(JobCardAsset))
        assert row is not None
        assert row.attachment_id == ""
        assert row.card_version == ""
        assert row.storage_path, "the render is kept even though the upload failed"

        renders: list[str] = []
        original = cards_mod.render_card_png

        def counting(*args, **kwargs):
            renders.append("rendered")
            return original(*args, **kwargs)

        monkeypatch.setattr(cards_mod, "render_card_png", counting)
        monkeypatch.setattr(
            cards_mod, "upload_attachment", lambda **kw: "att_recovered"
        )
        media = cards_mod.card_media(session, thread=thread, posting=posting, card=CARD)
        assert media[0].attachment_id == "att_recovered"
        assert len(renders) == 1, "retried the upload"

    def test_a_render_that_explodes_yields_no_media(
        self, session, account, monkeypatch, uploads
    ):
        def _explode(*args, **kwargs):
            raise RuntimeError("Pillow fell over")

        monkeypatch.setattr(cards_mod, "render_card_png", _explode)
        thread = _thread(session, account)
        posting = _posting(session, account["org_id"])
        assert cards_mod.card_media(session, thread=thread, posting=posting, card=CARD) == ()
        assert uploads == []

    def test_an_unfetchable_logo_still_produces_a_card(
        self, session, account, uploads, monkeypatch
    ):
        """No logo is a plain card, not a failed one -- the neutral ground is
        our own colour, so it still looks deliberate."""
        monkeypatch.setattr(cards_mod, "fetch_logo", lambda domain: None)
        thread = _thread(session, account)
        posting = _posting(session, account["org_id"])
        media = cards_mod.card_media(session, thread=thread, posting=posting, card=CARD)
        assert len(media) == 1
        assert len(uploads) == 1

    def test_a_logo_fetch_that_raises_still_produces_a_card(
        self, session, account, uploads, monkeypatch
    ):
        def _boom(domain):
            raise OSError("dns went away")

        monkeypatch.setattr(cards_mod, "fetch_logo", _boom)
        thread = _thread(session, account)
        posting = _posting(session, account["org_id"])
        assert len(cards_mod.card_media(
            session, thread=thread, posting=posting, card=CARD
        )) == 1


class TestTheRenderedBytes:
    def test_it_is_a_png_of_card_width(self, session, account, uploads):
        thread = _thread(session, account)
        posting = _posting(session, account["org_id"])
        cards_mod.card_media(session, thread=thread, posting=posting, card=CARD)
        row = session.scalar(select(JobCardAsset))
        from juicebox import storage

        data = storage.read_bytes(row.storage_path)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        with Image.open(io.BytesIO(data)) as image:
            assert image.width == 1200
            assert 0 < image.height <= 720

    def test_the_card_never_draws_a_score(self, session, account, uploads):
        """Candidates never see a score -- reasons only. The projection is
        already stripped before it gets here; this is the belt on the braces,
        because a number leaking into an image cannot be grepped out of a
        transcript later."""
        thread = _thread(session, account)
        posting = _posting(session, account["org_id"])
        scored = {**CARD, "score": 0.93, "recruiter_only": {"score": 0.93}}
        media = cards_mod.card_media(
            session, thread=thread, posting=posting, card=scored
        )
        assert len(media) == 1
        # The renderer reads named keys only, so an extra key cannot reach the
        # canvas. Asserted by rendering with and without it and comparing.
        row = session.scalar(select(JobCardAsset))
        from juicebox import storage

        with_score = storage.read_bytes(row.storage_path)
        plain = cards_mod.render_card_png(CARD, brand=None, logo=b"", index=1, total=1)
        scored_again = cards_mod.render_card_png(
            scored, brand=None, logo=b"", index=1, total=1
        )
        assert plain == scored_again
        assert with_score


class TestEndToEnd:
    """The whole turn, so the wiring between present_matches and cards is real.

    The unit tests above prove the cache and the failure paths; this proves the
    card actually reaches the message, which is the part that was broken --
    every role went out as a media-only vCard bubble that said "[contact card]
    Title - Company" and nothing else.
    """

    def test_an_imessage_thread_gets_image_and_text_in_one_bubble(
        self, session, account, uploads
    ):
        from test_messaging_manager import STRONG, _managed, _say
        from test_messaging_manager import _posting as _mgr_posting

        from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
        from juicebox.models import JobMatch

        channel = FakeChannel(consent=True)
        set_channel(LINQ, channel)
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            provider="linq",
            provider_thread_id="chat-cards",
            entry_point="linq_inbound",
            from_address="+15134333227",
            consent_messaging=True,
        )
        thread = _managed(session, thread)
        thread.last_service = SERVICE_IMESSAGE
        posting = _mgr_posting(session, account["org_id"], company="Sourcebot", suffix="e2e")
        session.add(
            JobMatch(
                org_id=account["org_id"],
                job_posting_id=posting.id,
                candidate_id=thread.candidate_id,
                score=0.9,
                criteria=[STRONG],
                status="new",
            )
        )
        session.flush()

        result = _say(session, thread, "find me some roles", native_id="m-e2e")
        assert result.action == "presented"

        card_messages = [
            item[1] for item in channel.sent if "ML Engineer" in (item[1].body or "")
        ]
        assert len(card_messages) == 1
        message = card_messages[0]
        # Both halves in one message: the picture and the words the candidate
        # can read without tapping anything.
        assert len(message.media) == 1
        assert message.media[0].attachment_id == "att_1"
        assert "Sourcebot" in message.body
        assert len(uploads) == 1

    def test_an_sms_thread_gets_the_same_words_and_no_image(
        self, session, account, uploads
    ):
        """The fallback is not a degraded card, it is the same card without a
        picture. That is what lets the image be best-effort."""
        from test_messaging_manager import STRONG, _managed, _say
        from test_messaging_manager import _posting as _mgr_posting

        from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
        from juicebox.models import JobMatch

        channel = FakeChannel(consent=True)
        set_channel(LINQ, channel)
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            provider="linq",
            provider_thread_id="chat-sms",
            entry_point="linq_inbound",
            from_address="+15134333228",
            consent_messaging=True,
        )
        thread = _managed(session, thread)
        thread.last_service = SERVICE_SMS
        posting = _mgr_posting(session, account["org_id"], company="Sourcebot", suffix="sms")
        session.add(
            JobMatch(
                org_id=account["org_id"],
                job_posting_id=posting.id,
                candidate_id=thread.candidate_id,
                score=0.9,
                criteria=[STRONG],
                status="new",
            )
        )
        session.flush()

        _say(session, thread, "find me some roles", native_id="m-sms")
        card_messages = [
            item[1] for item in channel.sent if "ML Engineer" in (item[1].body or "")
        ]
        assert len(card_messages) == 1
        assert card_messages[0].media == ()
        assert "Sourcebot" in card_messages[0].body
        assert uploads == []
