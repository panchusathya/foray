"""The submission seam: what it takes to actually send one application.

Shaped like the two provider seams that already work in this repo --
``integrations/providers.MailProvider`` and ``outreach/sender.EmailProvider``
-- because the rule those earned is the rule here: **adding a vendor must not
mean touching the caller.** The worker builds a ``SubmissionContext``, asks the
registry for an adapter, and records a ``SubmissionResult``. It does not know
what Workday is.

Two things a submitter must never do:

* **Guess.** ``needs_human`` exists so an adapter can stop. A required field we
  cannot answer from the resume, the preference row or the form answers is not
  something to invent on somebody's behalf (CANDIDATE_AGENT_PLAN §8).
* **Decide whether it is allowed to send.** Consent and the submission budget
  are checked in ``applications.service.request_submission`` and
  ``applications.state.transition`` before an adapter is ever reached. An
  adapter that re-derived those would be a second, subtly different gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..ats.detect import ApplyTarget

logger = logging.getLogger(__name__)

METHOD_EMAIL = "email"
METHOD_ATS_API = "ats_api"
METHOD_BROWSER = "browser"
METHOD_CONSOLE = "console"
METHOD_MEMORY = "memory"


class SubmitError(Exception):
    """An adapter failure worth retrying. Anything an adapter is sure about
    should come back as a ``SubmissionResult`` instead, so the attempt row
    records the verdict rather than an exception string."""


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    content: bytes

    def __len__(self) -> int:
        return len(self.content)


@dataclass
class SubmissionContext:
    """Everything an adapter is allowed to know, and nothing more.

    Deliberately a plain value object with the bytes already read: an adapter
    that took a ``Session`` would be one refactor away from moving the pursuit
    forward itself, and the status machine has to stay in one place.
    """

    application_id: str
    org_id: str
    candidate_id: str
    target: ApplyTarget

    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    #: The address the employer sees. An owned alias when one is issued,
    #: the candidate's own otherwise.
    from_address: str = ""
    #: Where relayed mail ultimately goes. Never given to an employer.
    reply_to_address: str = ""
    phone: str = ""
    linkedin_url: str = ""
    location: str = ""

    resume: Attachment | None = None
    cover_letter: Attachment | None = None
    #: ``[{key, question, answer, provenance, generated_by, edited}]`` from
    #: ``docgen.answers.build_form_answers``.
    answers: list[dict] = field(default_factory=list)

    company: str = ""
    title: str = ""
    posting_url: str = ""

    def answer(self, key: str) -> str:
        for entry in self.answers:
            if entry.get("key") == key:
                return str(entry.get("answer") or "")
        return ""

    @property
    def attachments(self) -> list[Attachment]:
        return [doc for doc in (self.resume, self.cover_letter) if doc is not None]


#: Keys inside ``SubmissionResult.verification`` that hold a credential. They
#: are encrypted before the row is written and decrypted only to hand back to
#: the adapter resuming the run -- ``applications/submission.py`` owns both
#: halves. A parked Workday attempt carries the portal password we generated,
#: and a column readable in a database dump is not where that belongs.
SECRET_VERIFICATION_KEYS = ("password",)


@dataclass
class SubmissionResult:
    """The verdict on one attempt.

    ``needs_verification`` and ``needs_human`` are both "not sent", but they
    are different futures: the first waits for mail at the alias and resumes,
    the second waits for a person and does not.
    """

    ok: bool = False
    method: str = ""
    external_id: str = ""
    confirmation_ref: str = ""
    needs_verification: bool = False
    needs_human: bool = False
    error: str = ""
    #: Storage-relative paths of whatever the adapter recorded.
    artifacts: list[str] = field(default_factory=list)
    #: Adapter-owned state carried across a parked attempt.
    verification: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        """The ``ApplicationSubmission.status`` this verdict implies."""
        if self.ok:
            return "succeeded"
        if self.needs_verification:
            return "needs_verification"
        if self.needs_human:
            return "needs_human"
        return "failed"


@runtime_checkable
class Submitter(Protocol):
    """One way of sending one application."""

    vendor: str
    method: str

    def submit(self, ctx: SubmissionContext) -> SubmissionResult:  # pragma: no cover - protocol
        ...


@runtime_checkable
class ResumableSubmitter(Protocol):
    """A submitter whose run can park and continue.

    Only the portals that make you register need this: the attempt stops with
    ``needs_verification`` and whatever state it needs in
    ``SubmissionResult.verification``, the alias mailbox receives the link or
    the code, and ``resume`` finishes the same attempt.
    """

    vendor: str
    method: str

    def submit(self, ctx: SubmissionContext) -> SubmissionResult:  # pragma: no cover - protocol
        ...

    def resume(
        self, ctx: SubmissionContext, verification: dict
    ) -> SubmissionResult:  # pragma: no cover - protocol
        ...
