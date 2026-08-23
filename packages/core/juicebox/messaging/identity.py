"""Who is on the other end.

A caller identifies themselves by the number they dialled from. A stranger in
a web chat identifies themselves by nothing at all, so a text thread has to ask
-- and a name and an email address are the two things it has to ask for that
the intake schema deliberately has no field for.

**They are not preferences and must never enter the spec.** ``ParsedQuery`` is
the vocabulary a job description is parsed into; a name has no counterpart on
the job side, so a ``contact_name`` key inside ``spec`` would be a field
``jobmatch.engine`` cannot score and would quietly turn the record back into
notes. They live on ``IntakeThread`` instead, and reach the domain as an
ordinary candidate row.

Extraction here is deliberately regex-only, with no model path. An address is
unambiguous, and a name arrives as the answer to the question that just asked
for one -- neither needs judgement, and both need to work with no API key.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from ..models import Candidate, IntakeThread
from ..repository import owner_of_email, upsert_candidates

logger = logging.getLogger(__name__)

# Pseudo-fields. Namespaced with a dot so they can never collide with an
# intake.schema key, which is what `asked_field` otherwise holds.
CONTACT_NAME = "contact.name"
CONTACT_EMAIL = "contact.email"

ASKS = {
    CONTACT_NAME: "First things first — what name should I put on this?",
    CONTACT_EMAIL: "And the best email to reach you on?",
}

# Set by _wrap_up when a conversation ends with nobody to attach it to, and
# cleared by attribute_thread when a human says who it was. One constant so the
# writer and the reader cannot drift into two different strings.
UNATTRIBUTED_REASON = "no contact details, preferences not attached to a candidate"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# "I'm", "my name is", "it's", "this is", "name:" and friends.
_NAME_LEAD = re.compile(
    r"^\s*(?:hi|hey|hello)?[,\s]*"
    r"(?:(?:my\s+name\s+is|i\s*am|i'm|its|it's|this\s+is|name)\s*[:\-]?\s*)?",
    re.IGNORECASE,
)
_NAME_TRAILING_PUNCT = re.compile(r"[\s.,;:!?]+$")

MAX_NAME_WORDS = 4
MAX_NAME_CHARS = 80


def find_email(body: str) -> str:
    match = _EMAIL.search(body or "")
    return match.group(0).strip().lower() if match else ""


def find_name(body: str) -> str:
    """A name, or empty when the text does not plainly look like one.

    Only ever called on the answer to the name question, so the bar is "does
    this look like a name" rather than "find a name in this sentence". Empty is
    the safe answer: it costs one repeat of an easy question, whereas a wrong
    name reaches a client on a profile.
    """
    text = _EMAIL.sub(" ", body or "")
    text = _NAME_LEAD.sub("", text, count=1)
    text = _NAME_TRAILING_PUNCT.sub("", text).strip()
    if not text or len(text) > MAX_NAME_CHARS:
        return ""
    if any(ch.isdigit() or ch == "@" for ch in text):
        return ""
    words = text.split()
    if not 1 <= len(words) <= MAX_NAME_WORDS:
        return ""
    if not all(any(ch.isalpha() for ch in word) for word in words):
        return ""
    return " ".join(words)


def absorb(thread: IntakeThread, *, body: str, asked_field: str) -> list[str]:
    """Take any contact detail out of an inbound message. Returns what changed.

    An address is read from *any* message, because somebody who volunteers one
    early should not be asked for it later. A name is read only when the last
    question asked for one -- otherwise "I'm looking for staff roles" becomes
    a candidate called "looking for staff roles".
    """
    changed: list[str] = []

    email = find_email(body)
    if email and not thread.contact_email:
        thread.contact_email = email[:255]
        changed.append(CONTACT_EMAIL)

    if not thread.contact_name and asked_field == CONTACT_NAME:
        name = find_name(body)
        if name:
            thread.contact_name = name[:240]
            changed.append(CONTACT_NAME)

    return changed


def missing_identity(thread: IntakeThread) -> tuple[str, ...]:
    """Contact pseudo-fields still outstanding, in the order to ask them.

    Returns every one that is missing rather than just the first, so the
    caller can skip past one it has already asked about twice. Neither has a
    ``declined`` equivalent -- there is no field on the record to mark -- which
    is exactly why the ask cap has to be applied from outside.
    """
    return tuple(
        key
        for key, value in ((CONTACT_NAME, thread.contact_name), (CONTACT_EMAIL, thread.contact_email))
        if not value
    )


def link_candidate(db: Session, *, thread: IntakeThread) -> str | None:
    """Resolve the thread to a candidate row, creating one if need be.

    Called once the required gate has closed, and again at wrap-up -- never
    on the way there. A public widget endpoint that created a candidate on the
    first inbound message would let anyone fill the CRM by opening a chat and
    typing once; requiring a completed gate makes that cost a whole intake.

    An existing owner of the address always wins. Creating a second row for
    somebody already in the CRM is worse than not creating one at all -- it
    splits their history and their preferences across two records.
    """
    if thread.candidate_id:
        return thread.candidate_id

    email = (thread.contact_email or "").strip().lower()
    name = (thread.contact_name or "").strip()
    if not email or not name:
        # Debug, not warning: the gate can close before the contact questions
        # do, so "not yet" is an ordinary intermediate state. The case that
        # actually matters -- a finished thread with nobody to attach it to --
        # is recorded on the thread by ``_wrap_up``.
        logger.debug(
            "intake thread %s not linkable yet: name=%s email=%s",
            thread.id,
            bool(name),
            bool(email),
        )
        return None

    existing = owner_of_email(db, org_id=thread.org_id, email=email)
    if existing:
        thread.candidate_id = existing
        return existing

    result = upsert_candidates(
        db,
        org_id=thread.org_id,
        payloads=[
            {
                "source": "intake_thread",
                "source_id": email,
                "full_name": name,
                "emails": [email],
            }
        ],
    )
    if not result.ids:
        return None
    thread.candidate_id = result.ids[0]
    return thread.candidate_id


class AttributionError(RuntimeError):
    """We will not attach this thread to that person. Safe to show a recruiter."""


def attribute_thread(db: Session, *, thread: IntakeThread, candidate_id: str) -> IntakeThread:
    """A human says who an unidentified conversation was.

    The counterpart of ``intake.service.attribute_call``, with one guard a call
    does not need: a thread often knows an email, and an address already owned
    by somebody else means the recruiter is about to staple this conversation to
    the wrong person. Refusing is the same rule ``AGENTS.md`` records for phone
    lookups -- a wrong match is a disclosure, not a miss.
    """
    candidate = db.get(Candidate, candidate_id)
    if candidate is None or candidate.org_id != thread.org_id:
        raise AttributionError("That candidate is not in this workspace.")

    email = (thread.contact_email or "").strip().lower()
    if email:
        owner = owner_of_email(db, org_id=thread.org_id, email=email)
        if owner and owner != candidate.id:
            raise AttributionError(
                f"{email} already belongs to somebody else. Merge the duplicate first."
            )

    thread.candidate_id = candidate.id

    # Whatever the conversation captured now has a person to hang off. Guarded
    # on there being something: attributing an empty thread must not write an
    # empty preference over a record another channel filled in.
    from ..intake.schema import coverage as coverage_of
    from .records import persist_preference

    if coverage_of(thread.record or {}).covered:
        persist_preference(db, thread=thread, candidate_id=candidate.id)

    if thread.review_reason == UNATTRIBUTED_REASON:
        thread.review_needed = False
        thread.review_reason = ""
    db.flush()
    return thread
