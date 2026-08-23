"""Candidate intake over text.

The same conversation the voice agent runs, on a channel where the other side
may take a week to answer. Everything that decides *what* to ask lives in
``juicebox.intake`` and is shared verbatim with voice; this package owns only
what is different about text:

* **cadence** -- voice asks until it may hang up, text asks one question per
  message and stops when asking again would get the number blocked;
* **durability** -- a call holds its state in the session, a thread holds it in
  ``IntakeThread.record`` and has to survive a week of silence;
* **idempotency** -- inbound message webhooks are retried, so the sender's own
  message id is the key;
* **consent** -- there is no recording to consent to, but there is TCPA.
"""

from __future__ import annotations

from .channels import (
    DeliveryResult,
    MessageChannel,
    OutboundMessage,
    channel_names,
    get_channel,
    reset_channels,
    set_channel,
)
from .conversation import TurnResult, run_turn
from .records import merge_record, persist_preference
from .threads import open_thread, record_inbound, record_outbound, transcript_of

__all__ = [
    "DeliveryResult",
    "MessageChannel",
    "OutboundMessage",
    "TurnResult",
    "channel_names",
    "get_channel",
    "merge_record",
    "open_thread",
    "persist_preference",
    "record_inbound",
    "record_outbound",
    "reset_channels",
    "run_turn",
    "set_channel",
    "transcript_of",
]
