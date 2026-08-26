"""Turning a voice memo into words.

The ``MailProvider`` / ``Submitter`` pattern again: a transcriber is the only
thing that knows a vendor's wire format, it never touches the database, and it
takes an injectable transport so the whole path is exercisable with no network
and no key.

Two failure classes, and the difference is load-bearing:

- ``TranscriptionUnavailable`` means *do not retry* — no key, a format we do
  not accept, a provider that refused the audio. The caller degrades to one
  reply asking the candidate to type it, and the thread carries on.
- ``TranscriptionError`` means *try again* — a 5xx, a timeout, a network blip.
  The caller lets it out so the queue's own backoff owns the retry.

Collapsing them into one exception would either retry a permanent refusal
forever or drop a recoverable one on the floor, and from the candidate's side
both look like being ignored.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

import httpx

from ..config import get_settings
from .linq import AUDIO_SUFFIXES

logger = logging.getLogger(__name__)

#: ``transport(method, url, json=..., data=...) -> dict``. The seam every
#: function in ``linq.py`` already has, widened by one argument because the
#: upload leg posts raw bytes rather than JSON.
Transport = Callable[..., dict[str, Any]]

ASSEMBLYAI_BASE = "https://api.assemblyai.com/v2"

#: How long a single transcript may take before we hand it back to the queue.
#: A voice memo is seconds of audio and AssemblyAI turns one round in a few
#: seconds; anything past this is a provider problem, not a long recording.
DEFAULT_TIMEOUT_SECONDS = 90
_POLL_INTERVAL_SECONDS = 2.0

#: Longer than any voice memo and short enough to stay one text. A candidate
#: who talks for four minutes gets the first part of it, which is better than
#: a wall the extractor reads worse than a paragraph.
MAX_TRANSCRIPT_CHARS = 4000


class TranscriptionError(RuntimeError):
    """Transcription failed in a way worth retrying."""


class TranscriptionUnavailable(TranscriptionError):
    """Transcription cannot succeed for this input. Do not retry."""


@runtime_checkable
class Transcriber(Protocol):
    """What a transcriber has to be able to do."""

    name: str

    def available(self) -> bool:
        """False when credentials are missing, so the caller can degrade with
        one honest reply rather than queueing work that cannot succeed."""
        ...

    def transcribe(self, data: bytes, *, mime_type: str, filename: str = "") -> str: ...


def supported(mime_type: str) -> bool:
    """Whether we accept this audio format at all."""
    return (mime_type or "").lower() in AUDIO_SUFFIXES


class NullTranscriber:
    """The no-key default. Refuses honestly instead of failing obscurely."""

    name = "null"

    def available(self) -> bool:
        return False

    def transcribe(self, data: bytes, *, mime_type: str, filename: str = "") -> str:
        raise TranscriptionUnavailable("no transcription provider is configured")


class AssemblyAITranscriber:
    """AssemblyAI, in three calls: upload, request, poll.

    The upload leg exists rather than handing AssemblyAI the Linq media URL
    directly. That URL is a signed link to a candidate's own recording, and
    passing it to a third party would widen who can fetch it — and it expires
    on Linq's schedule, not ours, so a retry after the queue backs off would
    find nothing there.
    """

    name = "assemblyai"

    def __init__(
        self,
        *,
        api_key: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        self._transport = transport
        self._sleep = sleep
        self._monotonic = monotonic

    def available(self) -> bool:
        # An injected transport IS the client and needs no key -- the same
        # reasoning linq._chat_action records, and the reason those courtesy
        # calls were once unreachable in tests.
        return bool(self._api_key or self._transport is not None)

    def transcribe(self, data: bytes, *, mime_type: str, filename: str = "") -> str:
        if not supported(mime_type):
            raise TranscriptionUnavailable(f"unsupported audio format {mime_type!r}")
        if not self.available():
            raise TranscriptionUnavailable("ASSEMBLYAI_API_KEY is not configured")
        if not data:
            raise TranscriptionUnavailable("no audio bytes to transcribe")

        upload = self._request("POST", f"{ASSEMBLYAI_BASE}/upload", data=data)
        audio_url = str(upload.get("upload_url") or "").strip()
        if not audio_url:
            raise TranscriptionError("AssemblyAI upload returned no upload_url")

        created = self._request(
            "POST",
            f"{ASSEMBLYAI_BASE}/transcript",
            json={"audio_url": audio_url, "speech_model": "universal"},
        )
        transcript_id = str(created.get("id") or "").strip()
        if not transcript_id:
            raise TranscriptionError("AssemblyAI accepted the audio but returned no id")

        return self._await_transcript(transcript_id, first=created)

    def _await_transcript(self, transcript_id: str, *, first: dict[str, Any]) -> str:
        deadline = self._monotonic() + self._timeout_seconds
        payload = first
        while True:
            status = str(payload.get("status") or "").strip().lower()
            if status == "completed":
                text = " ".join(str(payload.get("text") or "").split())
                if not text:
                    # Silence, a pocket-dial, or music. Nothing to act on and
                    # nothing a retry would change.
                    raise TranscriptionUnavailable("the recording had no speech in it")
                return text[:MAX_TRANSCRIPT_CHARS]
            if status == "error":
                detail = str(payload.get("error") or "unknown error")
                # The provider read the file and refused it. Retrying sends
                # the same bytes to the same rejection.
                raise TranscriptionUnavailable(f"AssemblyAI could not transcribe it: {detail}")
            if self._monotonic() >= deadline:
                # Raised, not degraded: the queue's backoff is the right owner
                # of "come back to this", and the transcript may well be ready
                # by then.
                raise TranscriptionError(
                    f"transcript {transcript_id} still {status!r} after "
                    f"{self._timeout_seconds}s"
                )
            self._sleep(_POLL_INTERVAL_SECONDS)
            payload = self._request("GET", f"{ASSEMBLYAI_BASE}/transcript/{transcript_id}")

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        data: bytes | None = None,
    ) -> dict[str, Any]:
        if self._transport is not None:
            return self._transport(method, url, json=json, data=data)
        headers = {"authorization": self._api_key}
        if data is not None:
            headers["content-type"] = "application/octet-stream"
        try:
            response = httpx.request(
                method, url, headers=headers, json=json, content=data, timeout=60.0
            )
            # 4xx is the provider telling us this input is wrong, and a retry
            # sends the identical request. 5xx is worth another go.
            if 400 <= response.status_code < 500:
                raise TranscriptionUnavailable(
                    f"AssemblyAI refused {method} {url}: "
                    f"{response.status_code} {(response.text or '')[:200]}"
                )
            response.raise_for_status()
            body = response.json()
        except TranscriptionError:
            raise
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"AssemblyAI {method} failed: {exc}") from exc
        except ValueError as exc:
            raise TranscriptionError(f"AssemblyAI returned malformed JSON: {exc}") from exc
        return body if isinstance(body, dict) else {}


#: An override, and nothing else. Deliberately not a cache of the configured
#: transcriber: ``get_settings`` is itself cached and tests reset it, so a
#: second cache here would go stale against the one the rest of the package
#: reads and the flag would appear not to work.
_override: Transcriber | None = None


def get_transcriber() -> Transcriber:
    """The configured transcriber, or ``NullTranscriber`` when there is none.

    Never raises. A missing key is a degrade path with a reply on the end of
    it, not a crash on the way into a webhook.
    """
    if _override is not None:
        return _override
    settings = get_settings()
    if not settings.intake_transcription_enabled or not settings.assemblyai_api_key:
        return NullTranscriber()
    return AssemblyAITranscriber(
        api_key=settings.assemblyai_api_key,
        timeout_seconds=settings.assemblyai_timeout_seconds,
    )


def set_transcriber(transcriber: Transcriber | None) -> None:
    """Install or clear an override. Tests use this the way they use
    ``channels.set_channel``."""
    global _override
    _override = transcriber


def reset_transcriber() -> None:
    """Drop the override so the next read goes back to settings."""
    global _override
    _override = None
