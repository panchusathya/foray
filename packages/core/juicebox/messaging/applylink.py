"""Applying to a role the candidate found themselves.

"Apply to this one for me" plus a link is the most obvious thing a person will
try, and until now it landed on the fallback deflection: ``manager._apply``
resolves an ordinal against ``thread.last_presented``, so a role we never
proposed had no ordinal to resolve.

Two separate ideas run this, and conflating them is the mistake to avoid:

**The gate** is what has to be on file before an application exists at all --
a name, a contact email, and a resume. It is a property of the candidate, so it
does not expire and answering it once serves every later application.

**The step budget** is what stops the agent nagging. The flow gets
``MAX_STEPS`` outbound messages to close the gate, and per item the existing
``MAX_ASKS_PER_FIELD`` still applies, so nothing is asked three times. Out of
budget is not a failure: the request is *parked*, the candidate keeps the portal
link, and a resume upload that lands next week resumes it.

**Nothing here submits.** It ends at ``create_application`` +
``request_package``, exactly where ``_apply`` ends, and every guardrail past
that point -- the recorded consent, the per-company budget, the one-per-posting
index, ``first_submission_gate`` -- is reached the same way it always was. A
second door into submission is the thing ``applications/service.py`` exists to
prevent.

``passes_bar`` is deliberately **not** consulted. The bar decides what *we*
propose; a candidate naming their own role has already made that call, and
telling somebody their own choice scored too low is not a product.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..applications.service import (
    ApplicationError,
    DuplicateApplicationError,
    create_application,
    request_package,
)
from ..jobmatch.fromurl import read_posting, upsert_from_url, urls_in
from ..models import Candidate, CandidateDocument, IntakeThread, JobPosting
from ..repository import is_placeholder_name
from . import identity, prompts

logger = logging.getLogger(__name__)

FLOW_META_KEY = "_apply_link"

STATUS_CONFIRMING = "confirming"
STATUS_GATHERING = "gathering"
STATUS_WAITING_RESUME = "waiting_resume"
STATUS_PARKED = "parked"
STATUS_DONE = "done"

#: Outbound messages this flow may spend closing the gate: the confirm, then up
#: to three asks. Past it the request parks with one message rather than asking
#: a fourth time -- one message too many is what gets a number blocked, which
#: is the cost ``AGENTS.md`` records against this whole channel.
MAX_STEPS = 4

#: Pseudo-fields on ``asked_field``, namespaced like ``contact.name`` and
#: ``batch.confirm`` so they cannot collide with an ``intake.schema`` key.
ASK_CONFIRM = "apply.confirm"
ASK_RESUME = "apply.resume"

NEED_EMAIL = "email"
NEED_NAME = "name"
NEED_RESUME = "resume"

#: What a parked request puts on the review queue. Never contains ``"; "`` --
#: ``identity._join_reasons`` uses that as its separator, so a reason holding
#: one splits into two segments and stops matching itself, which is how every
#: attributed call once stayed on the queue forever.
PARKED_REASON = "wants to apply to a role they sent, still missing something we need"


@dataclass(frozen=True)
class Pending:
    """The parked request, read off ``thread.record``."""

    url: str = ""
    posting_id: str = ""
    status: str = ""
    steps: int = 0

    def __bool__(self) -> bool:
        return bool(self.url) and self.status not in ("", STATUS_DONE)

    @property
    def waiting_on_resume(self) -> bool:
        return self.status == STATUS_WAITING_RESUME


# ---------------------------------------------------------------------------
# State on the thread
# ---------------------------------------------------------------------------


def pending(thread: IntakeThread) -> Pending:
    raw = (thread.record or {}).get(FLOW_META_KEY)
    if not isinstance(raw, dict):
        return Pending()
    return Pending(
        url=str(raw.get("url") or ""),
        posting_id=str(raw.get("posting_id") or ""),
        status=str(raw.get("status") or ""),
        steps=int(raw.get("steps") or 0),
    )


def remember(thread: IntakeThread, **fields: Any) -> Pending:
    """Reassign ``record`` so SQLAlchemy sees the JSON change.

    The same rule ``linkedin_flow.update_flow_meta`` and
    ``threads.record_outbound`` both document: an in-place edit to a JSON
    column is silently dropped, and here that would lose the step count and
    the flow would ask forever.
    """
    record = dict(thread.record or {})
    meta = dict(record.get(FLOW_META_KEY) or {})
    meta.update(fields)
    record[FLOW_META_KEY] = meta
    thread.record = record
    return pending(thread)


def clear(thread: IntakeThread) -> None:
    remember(thread, status=STATUS_DONE)
    _clear_review(thread)


def park(thread: IntakeThread) -> None:
    """Stop asking, and say so where a person will see it.

    Flagging matters as much as parking: an unanswered item stays unanswered
    (never ``declined`` -- recording a refusal nobody gave puts an invented
    answer into a record that matching reads), and a request nothing surfaces
    is a candidate who asked us to apply and silently never heard back.
    """
    remember(thread, status=STATUS_PARKED)
    from .identity import _join_reasons

    if PARKED_REASON not in (thread.review_reason or ""):
        thread.review_reason = _join_reasons(thread.review_reason or "", PARKED_REASON)
    thread.review_needed = True


def _clear_review(thread: IntakeThread) -> None:
    """Drop our reason once the request is resolved, leaving any others."""
    parts = [
        part
        for part in (thread.review_reason or "").split("; ")
        if part and part != PARKED_REASON
    ]
    thread.review_reason = "; ".join(parts)[:400]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def has_resume(db: Session, candidate: Candidate) -> bool:
    """A resume we can actually build a package from.

    An uploaded PDF, or a LinkedIn URL that produced employment history --
    ``service.build_package`` pays for the full history in that second case, so
    it is a real resume rather than a loophole. A candidate with neither has
    nothing for the generator to work from, and a package built from a blank
    career cannot be un-sent.
    """
    if candidate.experiences:
        return True
    uploaded = db.scalar(
        select(CandidateDocument).where(
            CandidateDocument.candidate_id == candidate.id,
            CandidateDocument.kind == "resume_uploaded",
        )
    )
    return uploaded is not None


def missing(db: Session, *, thread: IntakeThread, candidate: Candidate | None) -> tuple[str, ...]:
    """What is still outstanding, in the order it gets asked.

    Email first: it is the one that creates the candidate row, so nothing else
    can be checked until it lands.
    """
    if candidate is None:
        needed = [NEED_EMAIL]
        if not (thread.contact_name or "").strip():
            needed.append(NEED_NAME)
        return tuple(needed)
    gaps: list[str] = []
    if not (candidate.emails or []) and not (thread.contact_email or "").strip():
        gaps.append(NEED_EMAIL)
    # `is_placeholder_name`, not a blank check. `identity.link_candidate` names
    # a new row after the address it was created from so the CRM shows
    # something actionable, which means "has a name" is true the moment an
    # email arrives -- and an application addressed to `priya@example.com` is
    # exactly the thing to catch here rather than at the employer's end.
    if is_placeholder_name(candidate) and not (thread.contact_name or "").strip():
        gaps.append(NEED_NAME)
    if not has_resume(db, candidate):
        gaps.append(NEED_RESUME)
    return tuple(gaps)


_ASK_FIELD = {
    NEED_EMAIL: identity.CONTACT_EMAIL,
    NEED_NAME: identity.CONTACT_NAME,
    NEED_RESUME: ASK_RESUME,
}


def ask_field_for(need: str) -> str:
    return _ASK_FIELD.get(need, "")


def asked_too_often(thread: IntakeThread, need: str) -> bool:
    """Two asks per item and then stop — the cap ``conversation.py`` sets.

    ``AGENTS.md``: recording a refusal nobody gave puts an invented answer into
    a record that matching reads, so an unanswered item stays unanswered and
    the thread is flagged, never marked declined.
    """
    from .conversation import MAX_ASKS_PER_FIELD

    field = ask_field_for(need)
    counts = thread.asked_counts or {}
    return int(counts.get(field, 0)) >= MAX_ASKS_PER_FIELD


# ---------------------------------------------------------------------------
# Reading the link
# ---------------------------------------------------------------------------


def link_in(body: str) -> str:
    """The first URL in a message that could be a posting.

    Our own links are excluded: a candidate quoting the portal link we just
    sent them is not pasting a job, and ingesting it would create a posting out
    of our own page.
    """
    from ..config import get_settings

    settings = get_settings()
    ours = {
        _host(settings.base_url),
        _host(settings.portal_base_url),
    }
    ours.discard("")
    for url in urls_in(body):
        if _host(url) in ours:
            continue
        return url
    return ""


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url or "").netloc.lower().split(":")[0]


def ingest(db: Session, *, org_id: str, url: str) -> JobPosting | None:
    """Fetch the page, store the posting, resolve where applying goes.

    ``resolve_target`` gets its network hop here because somebody is actually
    applying, which is precisely the condition ``ats/detect.py`` documents for
    allowing one.
    """
    read = read_posting(url)
    if not read:
        return None
    posting = upsert_from_url(db, org_id=org_id, read=read, source_url=url)
    try:
        from ..applications.ats.detect import resolve_target

        resolve_target(db, posting, allow_network=True)
    except Exception as exc:  # noqa: BLE001 - a hand-off is a normal outcome
        logger.info("could not resolve an apply target for %s: %s", posting.id, exc)
    return posting


# ---------------------------------------------------------------------------
# Finishing
# ---------------------------------------------------------------------------


def submit_request(
    db: Session,
    *,
    thread: IntakeThread,
    posting: JobPosting,
    candidate_id: str,
    consenting_message_id: str | None,
    consent_verbatim: str,
) -> tuple[str, str]:
    """Create the application. Returns ``(body, action)`` for the caller to send.

    The one door: ``create_application`` owns the per-posting uniqueness and the
    per-company budget, and ``request_package`` queues the build that
    ``auto_submit`` runs off. Nothing new is gated here and nothing new submits.
    """
    from .manager import ACTION_ANSWERED, ACTION_APPLIED

    try:
        application = create_application(
            db,
            org_id=thread.org_id,
            candidate_id=candidate_id,
            job_posting_id=posting.id,
            source="candidate",
            consenting_message_id=consenting_message_id,
            consent_verbatim=(consent_verbatim or "")[:400],
        )
        request_package(db, application)
    except DuplicateApplicationError:
        return (
            prompts.APPLY_ALREADY_IN_FLIGHT.format(
                title=posting.title, company=posting.company
            ),
            ACTION_ANSWERED,
        )
    except ApplicationError:
        return (
            prompts.APPLY_PARKED.format(title=posting.title, company=posting.company),
            ACTION_ANSWERED,
        )
    return (
        prompts.APPLY_ACK.format(title=posting.title, company=posting.company),
        ACTION_APPLIED,
    )
