"""A voice memo becomes a turn.

A candidate on iMessage will answer "tell me what you're after" by talking, and
``_join_text_parts`` discarded every non-text part — so the most natural reply
to our own question was met with silence.

**This rides on the attachment path rather than beside it.** A voice memo *is*
an attachment, so it arrives on the same ``intake.attachment`` job
``messaging/attachments.py`` already owns. Audio never sniffs as a resume, so
the check happens one branch earlier than the resume loop: one dispatcher, one
download, and ``attachments``' rule that exactly one conversational consequence
follows every inbound message stays true. A second job kind for audio would
have raced that one and double-replied.

Two things here are easy to get wrong and both are load-bearing.

**The opt-out check runs on the transcript.** ``looks_like_stop`` reads
``IntakeMessage.body``, and an audio message's body is empty until this module
fills it in — so somebody who *says* "stop texting me" was never seen by the
keyword path at all. The transcript is checked here, before the turn is queued,
and a spoken STOP ends the thread with the one acknowledgement a carrier
expects.

**A refusal is not silence.** No key, an unsupported format, a recording with
no speech in it — each gets one plain reply asking the person to type it.
Silence is what this channel did before, and it reads as being ignored.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import IntakeMessage, IntakeThread
from . import prompts
from .channels import OutboundMessage
from .linq import InboundAttachment, is_audio
from .transcribe import TranscriptionError, TranscriptionUnavailable, get_transcriber

logger = logging.getLogger(__name__)

BODY_SOURCE_TRANSCRIPT = "transcript"


def audio_attachment(
    attachments: list[InboundAttachment],
) -> InboundAttachment | None:
    """The first attachment we would transcribe, or ``None``.

    Checked before the resume loop in ``ingest_inbound_attachments``: audio
    never sniffs as a resume, so letting that loop see it first would spend a
    download to learn nothing.
    """
    return next((a for a in attachments or [] if is_audio(a.content_type)), None)


def available() -> bool:
    """Whether transcription can succeed at all on this deployment."""
    return get_transcriber().available()


def decline(db: Session, *, thread: IntakeThread) -> IntakeMessage | None:
    """The one reply a voice memo we cannot read gets."""
    from .conversation import deliver

    return deliver(
        db, thread=thread, message=OutboundMessage(body=prompts.AUDIO_UNREADABLE)
    )


def transcribe_attachment(
    db: Session,
    *,
    thread: IntakeThread,
    message: IntakeMessage | None,
    attachment: InboundAttachment,
    data: bytes,
) -> dict:
    """Turn downloaded audio into the message's body, then say what follows.

    Returns a dict for the job row. ``turn`` True means the caller should
    enqueue the ordinary turn — the caller owns that, so "exactly one
    conversational consequence" stays decided in one place. Raises
    ``TranscriptionError`` for anything worth retrying, so the queue's backoff
    owns the retry rather than a loop in here.
    """
    from .suppression import handle_stop, looks_like_stop

    if message is not None and (message.body or "").strip():
        # Already transcribed. A redelivered job must not pay for it twice or
        # queue a second turn on the same words.
        return {"already_transcribed": True, "turn": True}

    try:
        text = get_transcriber().transcribe(
            data, mime_type=attachment.content_type, filename=attachment.filename
        )
    except TranscriptionUnavailable as exc:
        logger.info("voice memo on thread %s not transcribable: %s", thread.id, exc)
        decline(db, thread=thread)
        return {"declined": str(exc)}
    except TranscriptionError:
        # Deliberately out: the queue retries, and `fetch_media` refreshes an
        # expired signed URL on the way back in, so a late retry still has
        # bytes to work with.
        raise

    if message is None:
        # The row the transcript belongs on is gone. Nothing to write it to and
        # nothing for a turn to read, so stop rather than reply to a message
        # that no longer exists.
        logger.warning("transcribed audio for a message that vanished on %s", thread.id)
        return {"skipped": "message not found"}

    message.body = text
    message.body_source = BODY_SOURCE_TRANSCRIPT
    db.flush()

    if looks_like_stop(text):
        # A spoken opt-out is an opt-out. Same handling as the typed keyword
        # path in routers/messaging: suppress, acknowledge once, and never run
        # the turn -- `deliver` rather than `run_turn` because run_turn refuses
        # to send to a thread it has just stopped.
        from .conversation import deliver

        handle_stop(db, thread=thread)
        deliver(
            db, thread=thread, message=OutboundMessage(body=prompts.STOP_ACKNOWLEDGEMENT)
        )
        logger.info("spoken opt-out on thread %s", thread.id)
        return {"stopped": True}

    return {"transcribed": len(text), "turn": True}
