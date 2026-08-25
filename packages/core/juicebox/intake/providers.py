"""Voice platform adapters. The wire format stops here.

Same contract as ``integrations.providers``: a provider's payload shape may not
escape its adapter. Everything downstream sees a ``CallEvent``, so swapping
platforms — which has already happened once on paper — touches this file and
nothing else.

Retell signs both its post-call webhooks *and* its mid-call custom-function
requests with the same scheme, so one verifier covers both entry points. The
signature is the only thing standing between a public URL and a stranger writing
into somebody's org, because a phone call carries no principal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from ..config import get_settings
from .phones import lookup_key

logger = logging.getLogger(__name__)

RETELL_API_BASE = "https://api.retellai.com"

# What we do with each provider event. Retell emits more than this (transcript
# updates, the whole transfer lifecycle); anything unmapped becomes "ignored"
# rather than an error, because a provider adding an event type must not start
# failing our webhook.
# Fired while the phone is still ringing, before the agent says anything. It is
# the only point at which we can tell the agent who is calling, so it is what
# makes the dial-in line recognise a returning candidate instead of asking.
# Alone among the events it carries no call id -- the call does not exist yet --
# and alone among them the provider reads our *response*.
EVENT_INBOUND = "inbound_ringing"
EVENT_STARTED = "started"
EVENT_ENDED = "ended"
EVENT_ANALYZED = "analyzed"
EVENT_IGNORED = "ignored"

DIRECTION_INBOUND = "inbound"
DIRECTION_OUTBOUND = "outbound"


@dataclass(frozen=True)
class CallEvent:
    """One provider event, normalised."""

    kind: str
    provider_call_id: str
    direction: str = ""
    call_type: str = ""
    from_number: str = ""
    to_number: str = ""
    status: str = ""
    transcript: str = ""
    recording_url: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int = 0
    agent_id: str = ""
    dynamic_variables: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def our_number(self) -> str:
        """The number *we* own, which is what resolves the org.

        Depends on direction: on an inbound call the candidate dialled us, so
        ours is ``to_number``; on an outbound call we dialled them. A web call
        has no number at all and resolves by call id instead.
        """
        if self.direction == DIRECTION_INBOUND:
            return self.to_number
        if self.direction == DIRECTION_OUTBOUND:
            return self.from_number
        return ""

    @property
    def counterparty_number(self) -> str:
        """The candidate's number, for caller identification."""
        if self.direction == DIRECTION_INBOUND:
            return self.from_number
        if self.direction == DIRECTION_OUTBOUND:
            return self.to_number
        return ""


class SignatureError(RuntimeError):
    """The request did not come from the provider, or came too long ago."""


class ProviderCallError(RuntimeError):
    """The provider refused to place the call."""


@dataclass(frozen=True)
class PlacedCall:
    provider_call_id: str
    agent_id: str = ""
    status: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class VoiceProvider(Protocol):
    name: str

    def verify(self, *, raw_body: bytes, signature: str) -> None:
        """Raise ``SignatureError`` unless this request is authentic."""

    def parse_event(self, payload: dict[str, Any]) -> CallEvent:
        """Normalise a webhook body."""

    def parse_function_call(self, payload: dict[str, Any]) -> tuple[CallEvent, dict[str, Any]]:
        """Normalise a mid-call tool request into (event, args)."""

    def place_call(
        self,
        *,
        from_number: str,
        to_number: str,
        agent_id: str = "",
        metadata: dict[str, Any] | None = None,
        dynamic_variables: dict[str, str] | None = None,
    ) -> PlacedCall:
        """Dial out. Raises ``ProviderCallError`` rather than returning a failure."""


# --------------------------------------------------------------------------
# Retell
# --------------------------------------------------------------------------

# "v={unix_millis},d={hex_digest}"
_RETELL_SIGNATURE = re.compile(r"^v=(\d+),d=([0-9a-fA-F]+)$")

_RETELL_EVENTS = {
    "call_started": EVENT_STARTED,
    "call_ended": EVENT_ENDED,
    "call_analyzed": EVENT_ANALYZED,
}

# `call_inbound` puts its fields under their own key rather than under `call`,
# and the object holds only the three things known while the phone rings.
_RETELL_INBOUND_EVENT = "call_inbound"


class RetellProvider:
    """Retell AI.

    Signature scheme, from their docs: HMAC-SHA256 over ``raw_body`` followed by
    the timestamp string, keyed on the API key, hex encoded. Two details are easy
    to get wrong and both fail closed rather than loudly:

    * The digest is over the **raw bytes**. Re-serialising the parsed JSON
      changes whitespace and key order and every signature stops matching.
    * The API key must be the one flagged for webhooks in the Retell dashboard.
      A key that works for API calls can still fail every verification.
    """

    name = "retell"

    def __init__(self, *, api_key: str = "", tolerance_seconds: int | None = None) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.retell_api_key
        self._tolerance = (
            settings.retell_webhook_tolerance_seconds
            if tolerance_seconds is None
            else tolerance_seconds
        )

    def verify(self, *, raw_body: bytes, signature: str) -> None:
        if not self._api_key:
            # Refusing every request is the right failure: accepting unsigned
            # bodies because no key is configured turns a misconfiguration into
            # an open write endpoint that also spends money.
            raise SignatureError("no Retell API key configured; cannot verify webhooks")
        match = _RETELL_SIGNATURE.match((signature or "").strip())
        if match is None:
            raise SignatureError("malformed or missing X-Retell-Signature")

        timestamp, provided = match.group(1), match.group(2).lower()
        age_seconds = abs(_now_millis() - int(timestamp)) / 1000.0
        if age_seconds > self._tolerance:
            raise SignatureError(
                f"signature timestamp is {age_seconds:.0f}s old "
                f"(tolerance {self._tolerance}s)"
            )

        expected = hmac.new(
            self._api_key.encode("utf-8"),
            raw_body + timestamp.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, provided):
            raise SignatureError("signature mismatch")

    def sign(self, *, raw_body: bytes, timestamp_millis: int | None = None) -> str:
        """Produce a header for this body. Used by the tests, and by nothing else."""
        stamp = str(timestamp_millis if timestamp_millis is not None else _now_millis())
        digest = hmac.new(
            self._api_key.encode("utf-8"),
            raw_body + stamp.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"v={stamp},d={digest}"

    def parse_event(self, payload: dict[str, Any]) -> CallEvent:
        event = str(payload.get("event") or "")
        if event == _RETELL_INBOUND_EVENT:
            return self._inbound_to_event(payload)
        call = payload.get("call")
        call = call if isinstance(call, dict) else {}
        return self._call_to_event(_RETELL_EVENTS.get(event, EVENT_IGNORED), call)

    def _inbound_to_event(self, payload: dict[str, Any]) -> CallEvent:
        """The ringing event. Same ``CallEvent`` shape, almost all of it empty.

        ``direction`` is set by hand because the payload does not carry one: an
        inbound webhook is inbound by definition, and without it ``our_number``
        and ``counterparty_number`` would both return "" and the org would not
        resolve.

        The numbers go through ``lookup_key`` exactly as the lifecycle events
        do, so a withheld caller ID collapses to "" here too and the call is
        answered as a stranger's. That is the correct outcome and it needs no
        special case: nobody has been identified, so nothing is recalled.
        """
        inbound = payload.get(_RETELL_INBOUND_EVENT)
        inbound = inbound if isinstance(inbound, dict) else {}
        return CallEvent(
            kind=EVENT_INBOUND,
            # No call id exists yet. Retell mints one only once the call
            # connects, so anything keyed on it has to wait for `call_started`.
            provider_call_id="",
            direction=DIRECTION_INBOUND,
            call_type="phone_call",
            from_number=lookup_key(inbound.get("from_number"))
            or str(inbound.get("from_number") or ""),
            to_number=lookup_key(inbound.get("to_number")) or str(inbound.get("to_number") or ""),
            agent_id=str(inbound.get("agent_id") or ""),
            raw=payload,
        )

    def parse_function_call(self, payload: dict[str, Any]) -> tuple[CallEvent, dict[str, Any]]:
        """A custom-function request: ``{name, call, args}``.

        The ``call`` object carries the transcript *so far*, which is what lets
        the coverage gate be computed from the conversation rather than from the
        agent's opinion of it.
        """
        call = payload.get("call")
        call = call if isinstance(call, dict) else {}
        args = payload.get("args")
        args = args if isinstance(args, dict) else {}
        return self._call_to_event(EVENT_IGNORED, call), args

    def place_call(
        self,
        *,
        from_number: str,
        to_number: str,
        agent_id: str = "",
        metadata: dict[str, Any] | None = None,
        dynamic_variables: dict[str, str] | None = None,
    ) -> PlacedCall:
        """Dial a candidate.

        ``override_agent_id`` is used rather than relying on the number having an
        agent attached, because attaching one is what makes a bot answer real
        inbound callers. Outbound should not require taking that step.
        """
        if not self._api_key:
            raise ProviderCallError("no Retell API key configured")

        body: dict[str, Any] = {"from_number": from_number, "to_number": to_number}
        if agent_id:
            body["override_agent_id"] = agent_id
        if metadata:
            body["metadata"] = metadata
        if dynamic_variables:
            # Retell requires these to be strings.
            body["retell_llm_dynamic_variables"] = {
                str(k): str(v) for k, v in dynamic_variables.items()
            }

        try:
            response = httpx.post(
                f"{RETELL_API_BASE}/v2/create-phone-call",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise ProviderCallError(f"cannot reach Retell: {exc}") from exc

        if response.status_code >= 400:
            # Surfaced verbatim: the useful part is always in the body ("number
            # not owned", "destination not supported"), never the status line.
            raise ProviderCallError(f"retell {response.status_code}: {response.text[:300]}")

        payload = response.json() if response.content else {}
        call_id = str(payload.get("call_id") or "")
        if not call_id:
            raise ProviderCallError("retell accepted the call but returned no call_id")
        return PlacedCall(
            provider_call_id=call_id,
            agent_id=str(payload.get("agent_id") or ""),
            status=str(payload.get("call_status") or ""),
            raw=payload,
        )

    def _call_to_event(self, kind: str, call: dict[str, Any]) -> CallEvent:
        started = _from_millis(call.get("start_timestamp"))
        ended = _from_millis(call.get("end_timestamp"))
        duration = call.get("duration_ms")
        if isinstance(duration, (int, float)):
            seconds = int(duration // 1000)
        elif started and ended:
            seconds = max(0, int((ended - started).total_seconds()))
        else:
            seconds = 0

        return CallEvent(
            kind=kind,
            provider_call_id=str(call.get("call_id") or ""),
            direction=str(call.get("direction") or ""),
            call_type=str(call.get("call_type") or ""),
            # Normalised through the same key function the projection uses, so a
            # lookup cannot miss on formatting.
            from_number=lookup_key(call.get("from_number")) or str(call.get("from_number") or ""),
            to_number=lookup_key(call.get("to_number")) or str(call.get("to_number") or ""),
            status=str(call.get("call_status") or ""),
            transcript=str(call.get("transcript") or ""),
            recording_url=str(call.get("recording_url") or ""),
            started_at=started,
            ended_at=ended,
            duration_seconds=seconds,
            agent_id=str(call.get("agent_id") or ""),
            dynamic_variables=_dict(call.get("retell_llm_dynamic_variables")),
            metadata=_dict(call.get("metadata")),
            raw=call,
        )


class FakeVoiceProvider:
    """Test double. Accepts anything, or refuses everything.

    ``memory`` is to mail what this is to voice: the suite must be able to drive
    the whole path without a network or a signing key.
    """

    name = "fake"

    def __init__(self, *, accept: bool = True, dial_error: str = "") -> None:
        self.accept = accept
        self.dial_error = dial_error
        self.verified: list[bytes] = []
        self.placed: list[dict[str, Any]] = []

    def verify(self, *, raw_body: bytes, signature: str) -> None:
        self.verified.append(raw_body)
        if not self.accept:
            raise SignatureError("fake provider configured to reject")

    def parse_event(self, payload: dict[str, Any]) -> CallEvent:
        return RetellProvider(api_key="unused").parse_event(payload)

    def parse_function_call(self, payload: dict[str, Any]) -> tuple[CallEvent, dict[str, Any]]:
        return RetellProvider(api_key="unused").parse_function_call(payload)

    def place_call(
        self,
        *,
        from_number: str,
        to_number: str,
        agent_id: str = "",
        metadata: dict[str, Any] | None = None,
        dynamic_variables: dict[str, str] | None = None,
    ) -> PlacedCall:
        self.placed.append(
            {
                "from_number": from_number,
                "to_number": to_number,
                "agent_id": agent_id,
                "metadata": metadata or {},
                "dynamic_variables": dynamic_variables or {},
            }
        )
        if self.dial_error:
            raise ProviderCallError(self.dial_error)
        return PlacedCall(
            provider_call_id=f"fake_call_{len(self.placed)}",
            agent_id=agent_id,
            status="registered",
        )


_PROVIDERS: dict[str, Any] = {"retell": RetellProvider, "fake": FakeVoiceProvider}

_provider: VoiceProvider | None = None


def get_voice_provider() -> VoiceProvider:
    global _provider
    if _provider is None:
        name = get_settings().voice_provider
        factory = _PROVIDERS.get(name)
        if factory is None:
            logger.warning("unknown VOICE_PROVIDER %r, falling back to retell", name)
            factory = RetellProvider
        _provider = factory()
    return _provider


def set_voice_provider(provider: VoiceProvider | None) -> None:
    """Tests inject a provider; ``None`` restores the configured one."""
    global _provider
    _provider = provider


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _now_millis() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _from_millis(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def loads(raw_body: bytes) -> dict[str, Any]:
    """Parse a verified body. Returns ``{}`` rather than raising on garbage."""
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
