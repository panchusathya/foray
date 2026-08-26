"""Apply by link: a job URL texted in becomes a prepared application.

The candidate's side of the contract is one message — the link. Ours is the
filled application: posting ingested, package built (resume, cover letter,
form answers with provenance), and a portal link texted back by the existing
``EVENT_PACKAGE_READY`` path. Nothing is *submitted*: preparation is Tier 0
(CANDIDATE_AGENT_PLAN §8), and the consent recorded here is the message the
link arrived in — the strongest binding available, a specific posting named
by the candidate themselves.

The URL is stashed on the thread record (``_pending_apply``, the same meta
mechanism as ``linkedin_flow``'s ``_linkedin``) because the link routinely
arrives before we know who is texting: a stranger's first message can be a
job URL. Onboarding then runs as usual, and the stash is spent at hand-over —
``ensure_enqueued`` only fires once a candidate row exists, so the job never
runs for nobody and its message-scoped idempotency key stays permanent.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..applications.service import (
    ApplicationError,
    DuplicateApplicationError,
    create_application,
    request_package,
)
from ..jobmatch.ingest_url import LinkUnreachable, ingest_posting_from_url
from ..jobs import KIND_APPLY_LINK, Job, enqueue
from ..models import IntakeMessage, IntakeThread

logger = logging.getLogger(__name__)

APPLY_META_KEY = "_pending_apply"

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


def find_job_link(body: str) -> str:
    """The first URL in a message that could be a job posting.

    LinkedIn profile URLs are excluded: ``identity.absorb`` already reads
    those as "here is who I am", and treating one as "apply to this" would
    turn every onboarding reply into an application attempt.
    """
    for match in _URL_RE.finditer(body or ""):
        url = match.group(0).rstrip(".,;:!?'\"")
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.endswith("linkedin.com") and "/in/" in parsed.path:
            continue
        return url
    return ""


# ---------------------------------------------------------------------------
# the stash
# ---------------------------------------------------------------------------


def pending(thread: IntakeThread) -> dict[str, Any]:
    return dict((thread.record or {}).get(APPLY_META_KEY) or {})


def stash(thread: IntakeThread, *, url: str, message_id: str) -> None:
    record = dict(thread.record or {})
    record[APPLY_META_KEY] = {"url": url, "message_id": message_id}
    thread.record = record


def clear(thread: IntakeThread) -> None:
    record = dict(thread.record or {})
    if record.pop(APPLY_META_KEY, None) is not None:
        thread.record = record


def stash_from_body(thread: IntakeThread, *, body: str, message_id: str) -> bool:
    """Remember a job link seen mid-onboarding. Last link wins, like a
    corrected answer: the newest message is the one they mean."""
    url = find_job_link(body)
    if not url:
        return False
    stash(thread, url=url, message_id=message_id)
    return True


def ensure_enqueued(db: Session, thread: IntakeThread) -> Job | None:
    """Spend the stash once there is a candidate to apply for.

    Keyed on the message the link arrived in, so a retried webhook, a
    re-entered manager turn and the hand-over all collapse onto one job.
    """
    meta = pending(thread)
    if not meta.get("url") or not meta.get("message_id") or not thread.candidate_id:
        return None
    return enqueue(
        db,
        org_id=thread.org_id,
        kind=KIND_APPLY_LINK,
        idempotency_key=f"{KIND_APPLY_LINK}:{meta['message_id']}",
        payload={
            "thread_id": thread.id,
            "message_id": meta["message_id"],
            "url": meta["url"],
        },
    )


# ---------------------------------------------------------------------------
# the worker half
# ---------------------------------------------------------------------------


def handle_apply_link(db: Session, job: Job, now) -> dict[str, Any]:
    """Ingest the posting, open the application, queue the package.

    Every refusal texts the candidate why, through the notify path (canned
    bodies, cadence-gated): a link that vanishes into silence reads as the
    product losing their message, which is worse than any of the refusals.
    """
    from .notify import (
        EVENT_LINK_DUPLICATE,
        EVENT_LINK_PARKED,
        EVENT_LINK_PROBLEM,
        enqueue_notify,
    )

    thread = db.get(IntakeThread, job.payload.get("thread_id") or "")
    if thread is None:
        return {"skipped": "thread not found"}
    if thread.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    url = str(job.payload.get("url") or "")
    message_id = str(job.payload.get("message_id") or "")
    if not thread.candidate_id:
        # ensure_enqueued refuses without a candidate, so reaching here means
        # the row was unlinked after the fact. The stash survives; the next
        # turn that links a candidate re-enqueues.
        return {"skipped": "no candidate yet"}

    def _refuse(event: str, detail: str) -> dict[str, Any]:
        clear(thread)
        db.flush()
        enqueue_notify(db, thread=thread, event=event, subject_id=message_id)
        return {"skipped": detail, "notified": event}

    try:
        posting, created_posting = ingest_posting_from_url(
            db, org_id=job.org_id, url=url, now=now
        )
    except LinkUnreachable as exc:
        logger.info("apply link unreachable for thread %s: %s", thread.id, exc)
        return _refuse(EVENT_LINK_PROBLEM, "link unreachable")

    message = db.get(IntakeMessage, message_id)
    try:
        application = create_application(
            db,
            org_id=job.org_id,
            candidate_id=thread.candidate_id,
            job_posting_id=posting.id,
            source="link",
            consenting_message_id=message_id or None,
            consent_verbatim=(message.body if message is not None else url) or url,
        )
    except DuplicateApplicationError:
        return _refuse(EVENT_LINK_DUPLICATE, "already applied")
    except ApplicationError as exc:
        logger.info("apply link parked for thread %s: %s", thread.id, exc)
        return _refuse(EVENT_LINK_PARKED, "parked by limits")

    request_package(db, application)
    clear(thread)
    db.flush()
    return {
        "application_id": application.id,
        "job_posting_id": posting.id,
        "created_posting": created_posting,
    }
