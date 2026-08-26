"""The derived-spec cache on a posting, and the job that fills it.

``submit/browser/derive.py`` is the seeing half; this is the remembering half.
The spec lives on the posting rather than the application because the carding
gate consults it before any application exists, and one page view per posting
amortises across every candidate it is ever shown to.

Three states on ``JobPosting.form_spec``:

* ``None`` -- never checked. The only state that enqueues a derivation.
* a dict with ``"fields"`` -- usable; ``usable_spec`` returns the ``FormSpec``.
* ``{"unusable": <reason>}`` -- checked and definitively not derivable (an
  account wall, no form, nothing fillable). Final, like a recognised vendor.

A *transient* failure -- navigation error, timeout, a 403 from a datacentre
IP -- writes none of these. The job retries on the queue's backoff and parks
``failed`` when attempts run out, and ``enqueue`` revives a failed row the next
time a candidate matches the posting, so a bad afternoon never becomes a
permanent "no form here". Same lesson as ``resolve_target``'s
``stamp_on_miss=False``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from ..jobs import KIND_JOBS_FORMSPEC_ONE, Job, enqueue
from ..models import JobPosting, utcnow

logger = logging.getLogger(__name__)

UNUSABLE_KEY = "unusable"


def usable_spec(posting: JobPosting | None):
    """The posting's derived ``FormSpec``, or ``None``.

    ``None`` covers every other state -- never checked, checked-and-unusable,
    or a stored shape ``from_dict`` no longer recognises -- because every
    caller asks the same question: is there a form here we can honestly fill?
    """
    if posting is None or not isinstance(posting.form_spec, dict):
        return None
    if UNUSABLE_KEY in posting.form_spec:
        return None
    from .submit.browser.forms import FormSpec

    return FormSpec.from_dict(posting.form_spec)


def record_spec(db: Session, posting: JobPosting, spec, *, reason: str = "") -> None:
    """Write a definitive answer -- a usable spec, or why there is none.

    Only definitive answers land here; a caller with a transient failure
    simply does not call. ``form_spec_checked_at`` is the retry clock for
    package-time re-derivation, so it is stamped in both definitive cases.
    """
    if spec is not None:
        posting.form_spec = spec.to_dict()
    else:
        posting.form_spec = {UNUSABLE_KEY: (reason or "no derivable form")[:200]}
    posting.form_spec_checked_at = utcnow()
    db.flush()


def enqueue_derivation(db: Session, posting: JobPosting) -> Job | None:
    """Queue one derivation for a posting worth deriving, or ``None``.

    Only an *unrecognised* board qualifies. A vendor detect.py can name either
    has an adapter already, is account-gated (the two-leg flows are handwritten
    for a reason), or is a known wizard (iCIMS, Taleo) whose page-1 derivation
    would card a promise the one-page submitter cannot keep.

    Idempotent per posting forever -- ``enqueue`` returns the open row while
    one is queued or running, and revives a ``failed`` one, which is exactly
    the retry story we want: the next candidate match re-asks the question.
    """
    if posting.ats_vendor:
        return None
    if posting.form_spec is not None:
        return None
    url = (posting.apply_url or posting.source_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    return enqueue(
        db,
        org_id=posting.org_id,
        kind=KIND_JOBS_FORMSPEC_ONE,
        idempotency_key=f"{KIND_JOBS_FORMSPEC_ONE}:{posting.id}",
        payload={"posting_id": posting.id},
    )


def handle_formspec_one(db: Session, job: Job, now) -> dict[str, Any]:
    """Derive one posting's spec. The worker handler for ``jobs.formspec.one``.

    Raises on transient trouble so the queue's backoff owns the retry; returns
    normally only with a definitive answer written (or an honest skip).
    """
    posting = db.get(JobPosting, str(job.payload.get("posting_id") or ""))
    if posting is None or posting.org_id != job.org_id:
        return {"skipped": "posting not found"}
    if posting.form_spec is not None:
        return {"skipped": "already checked"}
    if posting.ats_vendor:
        # Recognised between enqueue and run -- the vendor answer wins.
        return {"skipped": f"vendor {posting.ats_vendor} resolved meanwhile"}
    url = (posting.apply_url or posting.source_url or "").strip()
    if not url.startswith(("http://", "https://")):
        record_spec(db, posting, None, reason="no reachable url")
        return {"posting_id": posting.id, "outcome": "unusable", "reason": "no url"}

    from .submit.browser.derive import inspect_form

    spec, questions = inspect_form(url)
    if spec is None:
        record_spec(db, posting, None, reason="no derivable form")
        return {"posting_id": posting.id, "outcome": "unusable"}
    record_spec(db, posting, spec)
    logger.info(
        "derived a form spec for posting %s: %d fields, %d questions seen",
        posting.id,
        len(spec.fields),
        len(questions),
    )
    return {
        "posting_id": posting.id,
        "outcome": "usable",
        "fields": sorted(spec.fields),
    }
