"""Historical derived-spec cache on a posting.

Public-form filling no longer reads ``JobPosting.form_spec``. The functions
here remain so older rows deserialize and leftover ``jobs.formspec.one`` jobs
skip cleanly.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..jobs import Job
from ..models import JobPosting, utcnow

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
    """No-op. Public-form carding no longer depends on a derived FormSpec.

    Question discovery is agent preflight at package time. Kept so existing
    job rows of ``jobs.formspec.one`` still have a handler.
    """
    del db, posting
    return None


def handle_formspec_one(db: Session, job: Job, now) -> dict[str, Any]:
    """Historical job kind. Derivation-as-fill is gone; skip remaining rows."""
    del now
    posting = db.get(JobPosting, str(job.payload.get("posting_id") or ""))
    if posting is None or posting.org_id != job.org_id:
        return {"skipped": "posting not found"}
    return {"skipped": "derived form specs are no longer used to fill"}
