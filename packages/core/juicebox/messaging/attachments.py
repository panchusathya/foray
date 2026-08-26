"""A resume that arrives ON the message channel, as an attachment.

The upload page (``uploads.py``) remains the route for channels that cannot
carry files — web chat — and the fallback when a download fails. But Linq is
iMessage: "send us your resume" invites an attachment, and the webhook hands
us a CDN URL for exactly that. Ignoring it (the old accepted gap in
docs/LINQ_SETUP.md) meant the most natural reply to our own question lost the
document.

Flow: the webhook records the inbound message and enqueues one
``intake.attachment`` job (never a turn — every path below guarantees the
conversation moves, so a second trigger would double-reply). The worker
downloads the file, sniffs it, and:

* **A resume (PDF/DOCX), thread linkable** → stored as a ``CandidateDocument``
  and parsed by the existing ``docs.parse`` job, which promotes the facts and
  settles the thread — the same path an upload-page resume takes.
* **A resume, thread not linkable yet** (no email or LinkedIn captured — a
  resume as the very first message) → the bytes are kept at a thread-scoped
  path and remembered in flow meta; the turn that later captures the email
  materializes the document (``materialize_pending``). The bytes must be
  taken NOW: Linq's ephemeral tier signs URLs with a 15-minute TTL, and
  "we'll fetch it once you answer" is how the file expires.
* **Not a resume** (a photo, a video) → if the thread is still waiting on a
  profile, say so and offer the upload link; otherwise hand back to the
  ordinary turn.

Candidate rows are still only created by ``identity.link_candidate`` under
its own rules. An attachment never manufactures an identity; it waits for
one.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..docgen.parse import SUFFIX_BY_MIME, sniff_resume_type
from ..jobs import KIND_DOC_PARSE, KIND_INTAKE_TURN, enqueue
from ..models import CandidateDocument, IntakeThread
from . import identity
from .linkedin_flow import flow_meta, update_flow_meta
from .linq import InboundAttachment, fetch_media

logger = logging.getLogger(__name__)

#: Flow-meta key holding a downloaded-but-unattached resume.
PENDING_KEY = "pending_resume"


def store_thread_resume(
    db: Session,
    *,
    thread: IntakeThread,
    candidate_id: str,
    data: bytes,
    filename: str,
    content_type: str = "",
    source: str = "upload",
) -> CandidateDocument:
    """One stored, default, parse-queued resume document for this thread.

    Shared by the upload page, the attachment path and the connector index so
    the default-flip, the id-built storage path and the parse enqueue cannot
    drift apart. ``source`` records which of the three it was; the kind stays
    ``resume_uploaded`` so every reader keeps working.
    """
    content_type = content_type or sniff_resume_type(data)
    suffix = SUFFIX_BY_MIME.get(content_type, ".pdf")
    doc = CandidateDocument(
        org_id=thread.org_id,
        candidate_id=candidate_id,
        kind="resume_uploaded",
        filename=(filename or f"resume{suffix}")[:255],
        content_type=content_type,
        status="uploaded",
        source=source,
        thread_id=thread.id,
    )
    db.execute(
        CandidateDocument.__table__.update()
        .where(
            CandidateDocument.candidate_id == candidate_id,
            CandidateDocument.kind == "resume_uploaded",
            CandidateDocument.is_default.is_(True),
        )
        .values(is_default=False)
    )
    doc.is_default = True
    db.add(doc)
    db.flush()

    relpath = storage.document_relpath(
        org_id=doc.org_id, candidate_id=doc.candidate_id, document_id=doc.id, suffix=suffix
    )
    size, digest = storage.save_bytes(relpath, data)
    doc.storage_path = relpath
    doc.size_bytes = size
    doc.sha256 = digest

    enqueue(
        db,
        org_id=thread.org_id,
        kind=KIND_DOC_PARSE,
        idempotency_key=f"{KIND_DOC_PARSE}:{doc.id}",
        payload={"document_id": doc.id},
    )
    return doc


def ingest_inbound_attachments(
    db: Session,
    *,
    thread: IntakeThread,
    message_id: str,
    attachments: list[InboundAttachment],
    had_text: bool,
    media_transport=None,
) -> dict[str, Any]:
    """The worker's whole job for one attachment-bearing inbound message.

    Returns a result dict for the job row. Exactly one conversational
    consequence follows every branch: a parse (which settles the thread), a
    pending stash plus a turn, a can't-read-that reply, or a plain turn for
    the message text.
    """
    resume_data: bytes | None = None
    resume_type = ""
    resume_name = ""
    download_failed = False

    for attachment in attachments:
        try:
            data = fetch_media(
                attachment.url,
                attachment_id=attachment.attachment_id,
                transport=media_transport,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, then the fallback copy
            logger.warning(
                "attachment download failed for thread %s: %s", thread.id, exc
            )
            download_failed = True
            continue
        kind = sniff_resume_type(data)
        if kind:
            resume_data = data
            resume_type = kind
            resume_name = attachment.filename
            break

    if resume_data is None:
        return _no_resume(
            db,
            thread=thread,
            message_id=message_id,
            had_text=had_text,
            download_failed=download_failed,
        )

    candidate_id = thread.candidate_id or identity.link_candidate(db, thread=thread)
    if candidate_id:
        doc = store_thread_resume(
            db,
            thread=thread,
            candidate_id=candidate_id,
            data=resume_data,
            filename=resume_name,
            content_type=resume_type,
        )
        return {"stored": doc.id, "content_type": resume_type}

    # Nobody to attach it to yet. Keep the bytes (the URL may be minutes from
    # expiry), remember them on the thread, and let the conversation run — its
    # next question is the email that makes the thread linkable.
    relpath = storage.pending_attachment_relpath(
        org_id=thread.org_id,
        thread_id=thread.id,
        message_id=message_id,
        suffix=SUFFIX_BY_MIME.get(resume_type, ".pdf"),
    )
    storage.save_bytes(relpath, resume_data)
    update_flow_meta(
        thread,
        **{
            PENDING_KEY: {
                "relpath": relpath,
                "filename": resume_name,
                "content_type": resume_type,
            }
        },
    )
    db.flush()
    _enqueue_turn(db, thread=thread, message_id=message_id)
    return {"pending": relpath, "content_type": resume_type}


def materialize_pending(db: Session, *, thread: IntakeThread) -> CandidateDocument | None:
    """Attach a held resume the moment the thread can own one.

    Called from the turn handler after ``identity.absorb`` — the earliest
    point an email can have just arrived. A no-op without a stash or without
    an identity, so calling it every turn costs nothing.
    """
    pending = flow_meta(thread).get(PENDING_KEY) or {}
    relpath = str(pending.get("relpath") or "")
    if not relpath:
        return None
    candidate_id = thread.candidate_id or identity.link_candidate(db, thread=thread)
    if not candidate_id:
        return None
    try:
        data = storage.read_bytes(relpath)
    except FileNotFoundError:
        # The stash is gone (retention sweep, disk move). Forget it rather
        # than re-failing forever; the conversation will ask again.
        update_flow_meta(thread, **{PENDING_KEY: {}})
        return None
    doc = store_thread_resume(
        db,
        thread=thread,
        candidate_id=candidate_id,
        data=data,
        filename=str(pending.get("filename") or ""),
        content_type=str(pending.get("content_type") or ""),
    )
    storage.delete_file(relpath)
    update_flow_meta(thread, **{PENDING_KEY: {}})
    db.flush()
    return doc


def _no_resume(
    db: Session,
    *,
    thread: IntakeThread,
    message_id: str,
    had_text: bool,
    download_failed: bool,
) -> dict[str, Any]:
    """No readable resume in the message. Keep the conversation honest."""
    from . import prompts
    from .channels import OutboundMessage
    from .conversation import deliver
    from .uploads import mint_upload_link

    # Only speak up when the file was plausibly meant as the answer to our
    # profile question; a random photo mid-conversation is not a turn.
    awaiting_profile = (
        not thread.contact_linkedin
        and not _thread_resume(db, thread)
        and not (flow_meta(thread).get(PENDING_KEY) or {})
    )
    if awaiting_profile and thread.status == "active":
        body = (
            prompts.ATTACHMENT_FAILED if download_failed else prompts.ATTACHMENT_UNREADABLE
        ).format(upload_link=mint_upload_link(thread))
        deliver(db, thread=thread, message=OutboundMessage(body=body))
        return {"replied": "unreadable_attachment", "download_failed": download_failed}
    if had_text:
        _enqueue_turn(db, thread=thread, message_id=message_id)
        return {"turn": True}
    return {"ignored": True}


def _thread_resume(db: Session, thread: IntakeThread) -> CandidateDocument | None:
    return db.scalar(
        select(CandidateDocument).where(
            CandidateDocument.thread_id == thread.id,
            CandidateDocument.kind == "resume_uploaded",
        )
    )


def _enqueue_turn(db: Session, *, thread: IntakeThread, message_id: str) -> None:
    enqueue(
        db,
        org_id=thread.org_id,
        kind=KIND_INTAKE_TURN,
        idempotency_key=f"{KIND_INTAKE_TURN}:{message_id}",
        payload={"thread_id": thread.id},
    )
