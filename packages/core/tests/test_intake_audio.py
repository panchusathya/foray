"""Inbound voice memos.

A voice memo rides on the same ``intake.attachment`` job a resume does, so most
of what is pinned here is the *routing*: audio is recognised before the resume
loop, and exactly one conversational consequence follows either way. A second
job kind for audio would have raced the attachment job and double-replied.

The rest is about what happens when transcription *cannot* work, because that
is the common case on an install with no key and it used to be
indistinguishable from the product ignoring somebody.

The one test here that is about compliance rather than product is
``test_a_spoken_stop_stops_the_thread``: ``looks_like_stop`` reads a message
body, and an audio message has none until the worker fills one in, so without
the check in ``audio.transcribe_attachment`` a spoken opt-out was never seen by
the keyword path at all.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from sqlalchemy import select

from juicebox.jobs import KIND_INTAKE_ATTACHMENT, KIND_INTAKE_TURN, Job
from juicebox.messaging import prompts
from juicebox.messaging.attachments import ingest_inbound_attachments
from juicebox.messaging.audio import audio_attachment
from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.linq import (
    AUDIO_SUFFIXES,
    InboundAttachment,
    is_audio,
    normalize_service,
    parse_inbound,
    sign_webhook,
)
from juicebox.messaging.threads import record_inbound
from juicebox.messaging.transcribe import (
    ASSEMBLYAI_BASE,
    AssemblyAITranscriber,
    NullTranscriber,
    TranscriptionError,
    TranscriptionUnavailable,
    get_transcriber,
    reset_transcriber,
    set_transcriber,
)
from juicebox.models import IntakeNumber, IntakeThread, Suppression

LINQ_WEBHOOK_SECRET = "whsec_" + base64.b64encode(b"linq-test-webhook-secret!!").decode()
OUR_NUMBER = "+14155559001"
CANDIDATE = "+14155550123"
CHAT_ID = "8f392755-6865-4b18-880a-227f9d8b458f"

AUDIO_URL = "https://cdn.linqapp.com/attachments/abc123?sig=deadbeef"
AUDIO_MIME = "audio/x-m4a"


def audio_part_payload(**overrides) -> dict:
    part = {
        "type": "media",
        "id": "att-1",
        "filename": "voice-memo.m4a",
        "mime_type": AUDIO_MIME,
        "size": 48_000,
        "url": AUDIO_URL,
    }
    part.update(overrides)
    return part


def payload_with(parts: list, *, message_id="msg-audio-1", service=None, **data_overrides) -> dict:
    data = {
        "chat_id": CHAT_ID,
        "from": CANDIDATE,
        "is_group": False,
        "message": {"id": message_id, "parts": parts},
        "recipient_handle": {"handle": OUR_NUMBER},
    }
    if service is not None:
        data["message"]["service"] = service
    data.update(data_overrides)
    return {
        "api_version": "v3",
        "event_type": "message.received",
        "event_id": f"evt-{message_id}",
        "data": data,
    }


def post_linq(client, payload):
    raw = json.dumps(payload).encode()
    ts = str(int(time.time()))
    signature = sign_webhook(
        raw_body=raw, webhook_id="wh-1", timestamp=ts, secret=LINQ_WEBHOOK_SECRET
    )
    return client.post(
        "/v1/intake/linq/webhook",
        content=raw,
        headers={
            "webhook-id": "wh-1",
            "webhook-timestamp": ts,
            "webhook-signature": signature,
            "Content-Type": "application/json",
        },
    )


@pytest.fixture
def linq_env(monkeypatch):
    monkeypatch.setenv("LINQ_WEBHOOK_SECRET", LINQ_WEBHOOK_SECRET)
    monkeypatch.setenv("LINQ_API_KEY", "linq_test_api_key")
    monkeypatch.setenv("LINQ_FROM_NUMBER", OUR_NUMBER)
    monkeypatch.setenv("LINQ_ENABLED", "true")
    from juicebox.config import reset_settings_cache
    from juicebox.messaging.channels import reset_channels

    reset_settings_cache()
    reset_channels()
    reset_transcriber()
    yield
    reset_settings_cache()
    reset_channels()
    reset_transcriber()


@pytest.fixture
def linq_number(session, account):
    row = IntakeNumber(
        org_id=account["org_id"], provider="linq", phone=OUR_NUMBER, label="linq intake"
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def linq_channel():
    """A FakeChannel on the Linq slot, so an outbound is recorded not sent."""
    channel = FakeChannel()
    set_channel(LINQ, channel)
    return channel


class StubTranscriber:
    """Records what it was handed and returns whatever it was told to."""

    name = "stub"

    def __init__(self, *, text="", raises=None, available=True):
        self.text = text
        self.raises = raises
        self._available = available
        self.calls: list[tuple[int, str]] = []

    def available(self) -> bool:
        return self._available

    def transcribe(self, data: bytes, *, mime_type: str, filename: str = "") -> str:
        self.calls.append((len(data), mime_type))
        if self.raises is not None:
            raise self.raises
        return self.text


# ---------------------------------------------------------------------------
# Recognising a voice memo
# ---------------------------------------------------------------------------


class TestRecognisingAudio:
    def test_an_audio_attachment_is_parsed_off_the_flat_shape(self):
        inbound = parse_inbound(payload_with([audio_part_payload()]))
        assert inbound.body == ""
        assert len(inbound.attachments) == 1
        assert inbound.audio is not None
        assert inbound.audio.url == AUDIO_URL
        assert inbound.audio.content_type == AUDIO_MIME
        assert inbound.audio.attachment_id == "att-1"

    def test_the_nested_2025_shape_parses_the_same(self):
        """The pinned subscription is 2026-02-03, but `parse_inbound` supports
        both and a media part must not be the thing that tells them apart."""
        flat = {
            "api_version": "v3",
            "event_type": "message.received",
            "data": {
                "id": "msg-flat",
                "chat": {"id": CHAT_ID, "owner_handle": OUR_NUMBER},
                "sender_handle": CANDIDATE,
                "service": "imessage",
                "parts": [audio_part_payload()],
            },
        }
        inbound = parse_inbound(flat)
        assert inbound.audio is not None
        assert inbound.service == "iMessage"

    def test_text_and_audio_together_keeps_both(self):
        inbound = parse_inbound(
            payload_with([{"type": "text", "value": "here you go"}, audio_part_payload()])
        )
        assert inbound.body == "here you go"
        assert inbound.audio is not None

    def test_a_photo_is_an_attachment_but_not_audio(self):
        inbound = parse_inbound(
            payload_with([audio_part_payload(mime_type="image/jpeg", url="https://x/y.jpg")])
        )
        assert len(inbound.attachments) == 1
        assert inbound.audio is None

    @pytest.mark.parametrize("mime", sorted(AUDIO_SUFFIXES))
    def test_every_format_linq_accepts_is_recognised(self, mime):
        assert is_audio(mime) is True

    def test_an_unlisted_format_is_not_audio(self):
        """`audio/ogg` is a real format Linq does not take, so recognising it
        would queue a download for a provider that will refuse the bytes."""
        assert is_audio("audio/ogg") is False
        assert is_audio("") is False

    def test_audio_attachment_picks_the_first_audio_of_several(self):
        photo = InboundAttachment(url="https://x/y.jpg", content_type="image/jpeg")
        memo = InboundAttachment(url=AUDIO_URL, content_type=AUDIO_MIME)
        assert audio_attachment([photo, memo]) is memo
        assert audio_attachment([photo]) is None
        assert audio_attachment([]) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("imessage", "iMessage"),
            ("iMessage", "iMessage"),
            ("RCS", "RCS"),
            ("sms", "SMS"),
            ("mms", "SMS"),
        ],
    )
    def test_service_is_normalised(self, raw, expected):
        assert normalize_service(raw) == expected

    def test_an_unknown_service_is_empty_not_a_guess(self):
        """Empty means "never told", which has to stay distinct from SMS --
        presentation falls back for both, but a guess would be wrong half the
        time and stored as though it were observed."""
        assert normalize_service("carrier-pigeon") == ""
        assert normalize_service(None) == ""


# ---------------------------------------------------------------------------
# The transcriber
# ---------------------------------------------------------------------------


class TestAssemblyAI:
    def _transport(self, script):
        calls = []

        def transport(method, url, *, json=None, data=None):
            calls.append((method, url, json, len(data or b"")))
            return script.pop(0)

        transport.calls = calls
        return transport

    def test_the_happy_path_is_upload_request_poll(self):
        transport = self._transport(
            [
                {"upload_url": "https://cdn.assemblyai.com/upload/xyz"},
                {"id": "t-1", "status": "queued"},
                {"id": "t-1", "status": "completed", "text": "  looking for  backend roles "},
            ]
        )
        t = AssemblyAITranscriber(transport=transport, sleep=lambda _: None)
        assert t.transcribe(b"audio-bytes", mime_type=AUDIO_MIME) == "looking for backend roles"
        assert [c[0] for c in transport.calls] == ["POST", "POST", "GET"]
        assert transport.calls[0][1] == f"{ASSEMBLYAI_BASE}/upload"
        assert transport.calls[0][3] == len(b"audio-bytes")
        assert transport.calls[1][2]["audio_url"] == "https://cdn.assemblyai.com/upload/xyz"

    def test_a_provider_error_status_does_not_retry(self):
        transport = self._transport(
            [
                {"upload_url": "u"},
                {"id": "t-1", "status": "error", "error": "file is not audio"},
            ]
        )
        t = AssemblyAITranscriber(transport=transport, sleep=lambda _: None)
        with pytest.raises(TranscriptionUnavailable):
            t.transcribe(b"x", mime_type=AUDIO_MIME)

    def test_a_recording_with_no_speech_does_not_retry(self):
        transport = self._transport(
            [{"upload_url": "u"}, {"id": "t-1", "status": "completed", "text": "   "}]
        )
        t = AssemblyAITranscriber(transport=transport, sleep=lambda _: None)
        with pytest.raises(TranscriptionUnavailable):
            t.transcribe(b"x", mime_type=AUDIO_MIME)

    def test_a_deadline_is_retryable_not_a_refusal(self):
        """The queue owns "come back to this". Degrading here would tell the
        candidate we gave up on a transcript that is probably ready by now."""
        clock = iter([0.0, 0.0, 1000.0])
        transport = self._transport(
            [
                {"upload_url": "u"},
                {"id": "t-1", "status": "queued"},
                {"id": "t-1", "status": "processing"},
            ]
        )
        t = AssemblyAITranscriber(
            transport=transport, sleep=lambda _: None, monotonic=lambda: next(clock)
        )
        with pytest.raises(TranscriptionError) as excinfo:
            t.transcribe(b"x", mime_type=AUDIO_MIME)
        assert not isinstance(excinfo.value, TranscriptionUnavailable)

    def test_an_unsupported_format_is_refused_before_any_call(self):
        transport = self._transport([])
        t = AssemblyAITranscriber(transport=transport)
        with pytest.raises(TranscriptionUnavailable):
            t.transcribe(b"x", mime_type="audio/ogg")
        assert transport.calls == []

    def test_a_long_transcript_is_capped(self):
        from juicebox.messaging.transcribe import MAX_TRANSCRIPT_CHARS

        transport = self._transport(
            [
                {"upload_url": "u"},
                {"id": "t-1", "status": "completed", "text": "word " * 5000},
            ]
        )
        t = AssemblyAITranscriber(transport=transport, sleep=lambda _: None)
        assert len(t.transcribe(b"x", mime_type=AUDIO_MIME)) <= MAX_TRANSCRIPT_CHARS


class TestTheDefaultIsNoTranscription:
    def test_with_no_key_the_null_transcriber_is_installed(self, linq_env):
        assert isinstance(get_transcriber(), NullTranscriber)
        assert get_transcriber().available() is False

    def test_the_flag_alone_is_not_enough(self, linq_env, monkeypatch):
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("INTAKE_TRANSCRIPTION_ENABLED", "true")
        reset_settings_cache()
        assert isinstance(get_transcriber(), NullTranscriber)

    def test_the_key_alone_is_not_enough(self, linq_env, monkeypatch):
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("ASSEMBLYAI_API_KEY", "aai_key")
        reset_settings_cache()
        assert isinstance(get_transcriber(), NullTranscriber)

    def test_flag_and_key_together_select_assemblyai(self, linq_env, monkeypatch):
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("INTAKE_TRANSCRIPTION_ENABLED", "true")
        monkeypatch.setenv("ASSEMBLYAI_API_KEY", "aai_key")
        reset_settings_cache()
        transcriber = get_transcriber()
        assert isinstance(transcriber, AssemblyAITranscriber)
        assert transcriber.available() is True

# ---------------------------------------------------------------------------
# Routing: the webhook and the attachment job
# ---------------------------------------------------------------------------


def _linq_thread(session, org_id, *, service="iMessage"):
    thread = IntakeThread(
        org_id=org_id,
        channel=LINQ,
        provider="linq",
        provider_thread_id=CHAT_ID,
        from_address=CANDIDATE,
        to_address=OUR_NUMBER,
        consent_messaging=True,
        last_service=service,
    )
    session.add(thread)
    session.flush()
    return thread


def _inbound(session, thread, *, body=""):
    message, _ = record_inbound(
        session, thread=thread, body=body, provider_native_id="msg-audio-1"
    )
    session.flush()
    return message


AUDIO_ATTACHMENT = InboundAttachment(
    url=AUDIO_URL,
    content_type=AUDIO_MIME,
    filename="voice-memo.m4a",
    size=48_000,
    attachment_id="att-1",
)


class TestTheWebhookRoutesAudioLikeAnyAttachment:
    def test_a_voice_memo_queues_the_attachment_job_and_not_a_turn(
        self, client, session, linq_number, linq_env
    ):
        """One dispatcher. A second job kind for audio would race the
        attachment job and double-reply."""
        response = post_linq(client, payload_with([audio_part_payload()]))
        assert response.status_code == 200
        assert response.json()["attachments"] is True

        kinds = [j.kind for j in session.scalars(select(Job)).all()]
        assert KIND_INTAKE_ATTACHMENT in kinds
        assert KIND_INTAKE_TURN not in kinds

    def test_the_job_carries_the_attachment_and_the_had_text_flag(
        self, client, session, linq_number, linq_env
    ):
        post_linq(
            client,
            payload_with([{"type": "text", "value": "here you go"}, audio_part_payload()]),
        )
        job = session.scalars(
            select(Job).where(Job.kind == KIND_INTAKE_ATTACHMENT)
        ).one()
        assert job.payload["had_text"] is True
        assert job.payload["attachments"][0]["content_type"] == AUDIO_MIME

    def test_the_observed_protocol_is_recorded_on_the_thread(
        self, client, session, linq_number, linq_env
    ):
        post_linq(client, payload_with([audio_part_payload()], service="imessage"))
        thread = session.scalars(select(IntakeThread)).one()
        assert thread.last_service == "iMessage"

    def test_a_text_message_is_unaffected(self, client, session, linq_number, linq_env):
        post_linq(client, payload_with([{"type": "text", "value": "senior backend in SF"}]))
        kinds = [j.kind for j in session.scalars(select(Job)).all()]
        assert KIND_INTAKE_TURN in kinds
        assert KIND_INTAKE_ATTACHMENT not in kinds

    def test_a_redelivered_voice_memo_queues_one_job(
        self, client, session, linq_number, linq_env
    ):
        assert post_linq(client, payload_with([audio_part_payload()])).status_code == 200
        second = post_linq(client, payload_with([audio_part_payload()]))
        assert second.json()["duplicate"] is True
        jobs = session.scalars(
            select(Job).where(Job.kind == KIND_INTAKE_ATTACHMENT)
        ).all()
        assert len(jobs) == 1


# ---------------------------------------------------------------------------
# The worker half
# ---------------------------------------------------------------------------


def _ingest(session, thread, message, *, data=b"fake-audio-bytes", attachment=None):
    """Run the attachment job the way the worker would."""
    return ingest_inbound_attachments(
        session,
        thread=thread,
        message_id=message.id if message is not None else "",
        attachments=[attachment or AUDIO_ATTACHMENT],
        had_text=False,
        media_transport=lambda url: (200, data),
    )


class TestTranscribingTheAttachment:
    def test_the_transcript_lands_on_the_row_and_queues_a_turn(
        self, session, account, linq_env, linq_channel
    ):
        thread = _linq_thread(session, account["org_id"])
        message = _inbound(session, thread)
        stub = StubTranscriber(text="I'm after senior backend roles in SF")
        set_transcriber(stub)

        result = _ingest(session, thread, message)
        assert result["transcribed"]
        assert message.body == "I'm after senior backend roles in SF"
        assert message.body_source == "transcript"
        assert stub.calls == [(len(b"fake-audio-bytes"), AUDIO_MIME)]

        jobs = session.scalars(select(Job).where(Job.kind == KIND_INTAKE_TURN)).all()
        assert len(jobs) == 1
        assert jobs[0].payload["thread_id"] == thread.id

    def test_audio_is_never_offered_to_the_resume_sniffer(
        self, session, account, linq_env, linq_channel
    ):
        """It would spend a download to learn nothing, and an m4a that happened
        to sniff as something would be stored as a resume."""
        thread = _linq_thread(session, account["org_id"])
        message = _inbound(session, thread)
        set_transcriber(StubTranscriber(text="hello"))
        _ingest(session, thread, message)
        from juicebox.models import CandidateDocument

        assert session.scalars(select(CandidateDocument)).all() == []

    def test_a_second_run_does_not_pay_for_it_again(
        self, session, account, linq_env, linq_channel
    ):
        thread = _linq_thread(session, account["org_id"])
        message = _inbound(session, thread)
        stub = StubTranscriber(text="hello")
        set_transcriber(stub)
        _ingest(session, thread, message)
        result = _ingest(session, thread, message)
        assert result["already_transcribed"] is True
        assert len(stub.calls) == 1

    def test_a_spoken_stop_stops_the_thread(
        self, session, account, linq_env, linq_channel
    ):
        """`looks_like_stop` reads the body, and an audio message has none
        until this point. Without the check here, a spoken opt-out never
        reached the keyword path at all."""
        thread = _linq_thread(session, account["org_id"])
        message = _inbound(session, thread)
        set_transcriber(StubTranscriber(text="stop"))

        result = _ingest(session, thread, message)
        assert result["stopped"] is True
        assert thread.status == "stopped"
        # Exactly one message after a STOP, the confirmation a carrier expects.
        assert linq_channel.bodies == [prompts.STOP_ACKNOWLEDGEMENT]
        # And no turn: the thread is finished, not answered.
        assert session.scalars(select(Job).where(Job.kind == KIND_INTAKE_TURN)).all() == []
        assert (
            session.scalars(
                select(Suppression).where(Suppression.phone == CANDIDATE)
            ).first()
            is not None
        )

    def test_a_provider_refusal_answers_once_and_queues_nothing(
        self, session, account, linq_env, linq_channel
    ):
        thread = _linq_thread(session, account["org_id"])
        message = _inbound(session, thread)
        set_transcriber(
            StubTranscriber(raises=TranscriptionUnavailable("no speech in it"))
        )
        result = _ingest(session, thread, message)
        assert result["declined"]
        assert linq_channel.bodies == [prompts.AUDIO_UNREADABLE]
        assert session.scalars(select(Job).where(Job.kind == KIND_INTAKE_TURN)).all() == []

    def test_a_retryable_failure_is_raised_for_the_queue(
        self, session, account, linq_env, linq_channel
    ):
        """Raising is what gets the backoff. Answering "type it instead" on a
        502 would give up on a transcript one retry away -- and `fetch_media`
        refreshes the signed URL on the way back in."""
        thread = _linq_thread(session, account["org_id"])
        message = _inbound(session, thread)
        set_transcriber(StubTranscriber(raises=TranscriptionError("502 from vendor")))
        with pytest.raises(TranscriptionError):
            _ingest(session, thread, message)
        assert linq_channel.bodies == []

    def test_a_download_failure_answers_rather_than_retrying(
        self, session, account, linq_env, linq_channel
    ):
        """`fetch_media` already refreshes an expired URL once, so a failure
        past that is not something another attempt fixes."""
        thread = _linq_thread(session, account["org_id"])
        message = _inbound(session, thread)
        set_transcriber(StubTranscriber(text="hello"))
        result = ingest_inbound_attachments(
            session,
            thread=thread,
            message_id=message.id,
            attachments=[AUDIO_ATTACHMENT],
            had_text=False,
            media_transport=lambda url: (410, b""),
        )
        assert result["replied"] == "audio_download_failed"
        assert linq_channel.bodies == [prompts.AUDIO_UNREADABLE]

    def test_with_no_transcriber_audio_falls_through_to_the_ordinary_path(
        self, session, account, linq_env, linq_channel
    ):
        """No key means no transcription, and the attachment path then treats a
        voice memo like any other unreadable file -- one honest reply while the
        profile question is open, never silence."""
        thread = _linq_thread(session, account["org_id"])
        message = _inbound(session, thread)
        reset_transcriber()
        result = _ingest(session, thread, message)
        assert "transcribed" not in result
        assert len(linq_channel.bodies) == 1
        assert message.body == ""
