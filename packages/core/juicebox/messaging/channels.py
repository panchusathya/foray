"""Where an outbound intake message actually goes.

The ``MailProvider`` pattern in ``integrations/providers.py``, applied to
messaging: a channel is the only thing that knows a vendor's wire format, and
nothing downstream can tell which one delivered a message. The conversation
logic in ``conversation.py`` never imports a vendor.

A channel does not touch the database. ``threads.py`` owns persistence, so a
channel that fails leaves a row recording the failure rather than a half-
written thread -- and a test can swap in ``FakeChannel`` without a session.

**Consent belongs to the channel, not the caller.** Web chat is opened by the
person typing into it, so it needs none. SMS needs prior express consent under
TCPA before the first outbound message, and the only safe place to enforce
that is here, below every code path that could send. ``requires_consent`` is
the switch, and ``send`` is expected to refuse rather than warn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..config import get_settings
from .linq import LinqSendError, OutboundMedia, send_message, stop_typing

WEB = "web"
LINQ = "linq"
FAKE = "fake"


class ChannelError(Exception):
    """Any delivery-side failure, so callers need not know the vendor."""

    def __init__(self, detail: str, *, status_code: int = 0) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class OutboundMessage:
    """One message we intend to send.

    ``asked_field`` is the intake field this message is a question about, and
    is empty for a greeting or a sign-off. It is carried to the stored row so
    an answer can be bound to a question, and so the same field is never asked
    twice running.
    """

    body: str
    asked_field: str = ""
    # Stable key for vendor send idempotency. ``conversation._deliver`` fills
    # ``{thread_id}:{seq}`` when blank so a retry reuses the same key.
    send_key: str = ""
    # Skip the channel's lowercase voice transform. Set it when the body is
    # mostly quoted data rather than our own prose -- a job card carries an
    # employer's name and a role title lifted from a posting, and rendering
    # "ml engineer at anthropic" makes a proper noun look like a typo and the
    # card harder to skim. Prose stays lowercase; other people's names do not.
    verbatim: bool = False
    # Attachments to send alongside the text, as already-uploaded Linq
    # attachment ids or public URLs. A channel that cannot carry media ignores
    # them and still delivers the body: the text has to stand on its own,
    # because an image that fails to render must not take the message with it.
    media: tuple[OutboundMedia, ...] = ()


@dataclass(frozen=True)
class DeliveryResult:
    """What the channel did with it.

    ``provider_native_id`` is the vendor's own message id where there is one.
    Blank is legitimate -- web chat has no vendor -- and the partial unique
    index on ``intake_messages`` is partial precisely so blank does not
    collide with blank.
    """

    status: str = "sent"
    provider_native_id: str = ""
    error: str = ""


@runtime_checkable
class MessageChannel(Protocol):
    """What a channel has to be able to do.

    An SMS adapter is this protocol plus a webhook parser that turns the
    vendor's inbound payload into ``(provider_native_id, from_address,
    to_address, body)``. It must supply the vendor's message id: a channel
    that cannot is a channel that cannot be idempotent, and inbound webhooks
    are always retried.
    """

    name: str

    def is_configured(self) -> bool:
        """False when credentials are missing, so a route can 503 rather than
        half-open a conversation it cannot continue."""
        ...

    def requires_consent(self) -> bool:
        """True where a regulator requires prior express consent to send."""
        ...

    def send(
        self,
        *,
        to_address: str,
        message: OutboundMessage,
        provider_thread_id: str = "",
    ) -> DeliveryResult: ...


class WebChatChannel:
    """The browser widget. There is no vendor and nothing leaves the process.

    Delivery is the HTTP response: the route persists the outbound row and
    hands it straight back to the page that is waiting for it. ``send`` exists
    so the conversation logic has one shape to call, not because anything
    needs transmitting.
    """

    name = WEB

    def is_configured(self) -> bool:
        return True

    def requires_consent(self) -> bool:
        # The person opened the widget and typed into it. There is no
        # unsolicited message to consent to, and no carrier in the path.
        return False

    def send(
        self,
        *,
        to_address: str,
        message: OutboundMessage,
        provider_thread_id: str = "",
    ) -> DeliveryResult:
        return DeliveryResult(status="sent")


class LinqChannel:
    """Linq iMessage / RCS / SMS. Outbound replies go to an existing chat."""

    name = LINQ

    def is_configured(self) -> bool:
        settings = get_settings()
        return bool(settings.linq_api_key and settings.linq_from_number)

    def requires_consent(self) -> bool:
        return True

    def send(
        self,
        *,
        to_address: str,
        message: OutboundMessage,
        provider_thread_id: str = "",
    ) -> DeliveryResult:
        if not provider_thread_id:
            raise ChannelError("Linq send requires provider_thread_id (chat id)")
        # Drop the typing indicator so iMessage does not keep showing it after
        # the reply lands. Failures are swallowed inside stop_typing.
        stop_typing(provider_thread_id)
        try:
            message_id = send_message(
                chat_id=provider_thread_id,
                body=message.body,
                idempotency_key=message.send_key or message.asked_field or message.body[:255],
                media=message.media,
            )
        except LinqSendError as exc:
            raise ChannelError(str(exc)) from exc
        return DeliveryResult(status="sent", provider_native_id=message_id)


class FakeChannel:
    """Records what would have been sent. The ``MemorySender`` of messaging.

    Tests assert on ``sent`` rather than on a transport, which is what lets the
    whole conversation be exercised with no vendor and no network.
    """

    name = FAKE

    def __init__(self, *, configured: bool = True, consent: bool = False) -> None:
        self._configured = configured
        self._consent = consent
        self.sent: list[tuple[str, OutboundMessage]] = []
        self.fail_next: str = ""

    def is_configured(self) -> bool:
        return self._configured

    def requires_consent(self) -> bool:
        return self._consent

    def send(
        self,
        *,
        to_address: str,
        message: OutboundMessage,
        provider_thread_id: str = "",
    ) -> DeliveryResult:
        if self.fail_next:
            detail, self.fail_next = self.fail_next, ""
            raise ChannelError(detail)
        self.sent.append((to_address, message, provider_thread_id))
        return DeliveryResult(
            status="sent", provider_native_id=f"fake-{len(self.sent)}"
        )

    @property
    def bodies(self) -> list[str]:
        return [item[1].body for item in self.sent]


_channels: dict[str, MessageChannel] = {}
_defaults: dict[str, type] = {WEB: WebChatChannel, LINQ: LinqChannel, FAKE: FakeChannel}


def _load_defaults() -> None:
    for name, factory in _defaults.items():
        _channels.setdefault(name, factory())


def get_channel(name: str) -> MessageChannel:
    """Look up a channel by ``IntakeThread.channel``.

    An unknown name is a hard error, never a silent default. Falling back to
    web here would drop a text on the floor while reporting success, which is
    the same reasoning ``integrations.providers.get_provider`` gives.
    """
    if not _channels:
        _load_defaults()
    try:
        return _channels[name]
    except KeyError:
        raise ChannelError(f"unknown message channel {name!r}") from None


def channel_names() -> list[str]:
    if not _channels:
        _load_defaults()
    return sorted(_channels)


def set_channel(name: str, channel: MessageChannel | None) -> None:
    """Install or remove a channel. Tests use this to inject ``FakeChannel``."""
    if not _channels:
        _load_defaults()
    if channel is None:
        _channels.pop(name, None)
        return
    _channels[name] = channel


def reset_channels() -> None:
    """Drop every override. Called between tests so one cannot leak forward."""
    _channels.clear()
    _load_defaults()
