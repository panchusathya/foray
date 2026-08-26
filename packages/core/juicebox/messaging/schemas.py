"""Request and response shapes for the chat endpoints.

In this package rather than the shared ``juicebox/schemas.py`` for the same
reason the prompts are: that file is one the voice branch is also appending to,
and nothing outside messaging needs these. They belong there once the branches
have met.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ChatStartIn(BaseModel):
    """Opening a conversation.

    One opaque token, not an org id and a signature side by side. Splitting
    them into two fields invites a caller -- or a later refactor -- to read the
    org from the request and check the signature separately, which is the exact
    mistake the signature exists to make impossible.
    """

    token: str = Field(min_length=1, description="Signed widget token: <org_id>.<signature>")
    # A stable, widget-generated browser id (localStorage). It is what lets a
    # STOP outlive the session that said it: a stopped thread binds to
    # (channel, from_address), and a widget thread without one has no
    # from_address — so a new session used to start asking questions again.
    # Optional because old embeds don't send it; those keep today's behavior.
    visitor_id: str = Field(default="", max_length=200)


class ChatMessageIn(BaseModel):
    """One inbound message.

    ``client_message_id`` is required, not optional. It is the idempotency key,
    and an optional idempotency key is one that is missing exactly when a
    retry storm makes it matter.
    """

    token: str = Field(min_length=1, description="Thread token from the start response")
    body: str = Field(min_length=1, max_length=4000)
    client_message_id: str = Field(min_length=1, max_length=200)


class ChatMessageOut(BaseModel):
    id: str
    seq: int
    direction: str
    body: str
    asked_field: str = ""
    created_at: datetime | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    read_at: datetime | None = None

    model_config = {"from_attributes": True}


class ChatSessionOut(BaseModel):
    """What the widget needs to render the conversation and keep posting."""

    thread_id: str
    thread_token: str
    status: str
    action: str = ""
    required_complete: bool = False
    # What the gate still owes, so the widget can show progress without
    # recomputing it. Never a score or a judgement -- see the guardrails.
    fields_missing: list[str] = Field(default_factory=list)
    messages: list[ChatMessageOut] = Field(default_factory=list)


class WidgetTokenOut(BaseModel):
    """The embeddable token for an org. Recruiter-authenticated."""

    token: str
