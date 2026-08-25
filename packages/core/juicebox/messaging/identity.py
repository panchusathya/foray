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

from ..enrich.linkedin_url import canonical_linkedin_url
from ..enrich.linkedin_url import find_linkedin as extract_linkedin
from ..models import Candidate, IntakeThread
from ..repository import owner_of_email, owner_of_linkedin, upsert_candidates

logger = logging.getLogger(__name__)


def _mark_crm_tracked(db: Session, candidate_id: str) -> None:
    """Intake-linked people belong on the default Contacts grid."""
    # Late import: crm -> outreach -> sequencer -> crm would cycle if
    # messaging.identity pulled track at module load.
    from ..crm import track

    candidate = db.get(Candidate, candidate_id)
    if candidate is not None:
        track(candidate)
        db.flush()

# Pseudo-fields. Namespaced with a dot so they can never collide with an
# intake.schema key, which is what `asked_field` otherwise holds.
CONTACT_NAME = "contact.name"
CONTACT_EMAIL = "contact.email"
CONTACT_LINKEDIN = "contact.linkedin"

ASKS = {
    CONTACT_NAME: "first things first — what name should i put on this?",
    CONTACT_EMAIL: "and the best email to reach you on?",
    # Not "first" -- the intro already used that word for the email question,
    # and hearing it twice in consecutive messages reads as a restart.
    CONTACT_LINKEDIN: "got it. now drop your linkedin url (or just the /in/ handle)?",
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


def find_linkedin(body: str) -> str:
    """Canonical ``https://www.linkedin.com/in/{slug}``, or empty."""
    return extract_linkedin(body)


# One batched question gets one batched answer: "Sathya, 2 yrs, 150k". Every
# segment is still judged by ``find_name`` -- this only decides where to look,
# it does not lower the bar for what counts as a name.
_BATCH_SPLIT = re.compile(r"[,;\n]")


def find_name_in_batch(body: str) -> str:
    """A name from a comma-separated answer to several questions at once.

    ``find_name`` refuses anything containing a digit, which is right for a
    single answer and wrong here: the gap-fill asks for name, years and comp
    at once, so "Sathya, 2 yrs, 150k" loses the name entirely.

    Only the FIRST segment is considered, because the name line is always the
    first question in ``gap_fill_message``. Scanning every segment for the
    first thing that looks like a name reads "150k, asap" as a candidate
    called Asap -- answering the other questions and skipping this one would
    put a wrong name on a profile that goes in front of employers. Failing
    here costs one repeat of an easy question, which is the same trade
    ``find_name`` already makes.
    """
    first = _BATCH_SPLIT.split(body or "", maxsplit=1)[0]
    return find_name(first)


def absorb(
    thread: IntakeThread, *, body: str, asked_field: str, batched: bool = False
) -> list[str]:
    """Take any contact detail out of an inbound message. Returns what changed.

    An address and a LinkedIn URL are read from *any* message, because somebody
    who volunteers one early should not be asked for it later. A name is read
    only when the last question asked for one -- otherwise "I'm looking for
    staff roles" becomes a candidate called "looking for staff roles".

    ``batched`` says the last question was a multi-part one, so the answer is
    read segment by segment. The caller owns that fact; passing it here rather
    than matching on a pseudo-field keeps this module unaware of which flow is
    running.
    """
    changed: list[str] = []

    email = find_email(body)
    if email and not thread.contact_email:
        thread.contact_email = email[:255]
        changed.append(CONTACT_EMAIL)

    linkedin = find_linkedin(body)
    if linkedin and not thread.contact_linkedin:
        thread.contact_linkedin = linkedin[:400]
        changed.append(CONTACT_LINKEDIN)

    if not thread.contact_name and (asked_field == CONTACT_NAME or batched):
        name = find_name_in_batch(body) if batched else find_name(body)
        if name:
            thread.contact_name = name[:240]
            changed.append(CONTACT_NAME)

    return changed


def missing_identity(thread: IntakeThread, *, linkedin_first: bool = False) -> tuple[str, ...]:
    """Contact pseudo-fields still outstanding, in the order to ask them.

    Returns every one that is missing rather than just the first, so the
    caller can skip past one it has already asked about twice. Neither has a
    ``declined`` equivalent -- there is no field on the record to mark -- which
    is exactly why the ask cap has to be applied from outside.

    LinkedIn-first threads ask for a URL (and a name if the scrape did not
    supply one) and skip email: a public widget that required an address
    before matching would stall people who only texted a profile, and the
    CRM can hang off ``linkedin_url``.
    """
    if linkedin_first:
        items = (
            (CONTACT_LINKEDIN, thread.contact_linkedin),
            (CONTACT_NAME, thread.contact_name),
        )
    else:
        items = (
            (CONTACT_NAME, thread.contact_name),
            (CONTACT_EMAIL, thread.contact_email),
        )
    return tuple(key for key, value in items if not value)


def link_candidate(db: Session, *, thread: IntakeThread) -> str | None:
    """Resolve the thread to a candidate row, creating one if need be.

    Called once the required gate has closed, and again at wrap-up -- never
    on the way there. A public widget endpoint that created a candidate on the
    first inbound message would let anyone fill the CRM by opening a chat and
    typing once; requiring a completed gate makes that cost a whole intake.

    An existing owner of the address or LinkedIn URL always wins. Creating a
    second row for somebody already in the CRM is worse than not creating one
    at all -- it splits their history and their preferences across two records.
    """
    if thread.candidate_id:
        _mark_crm_tracked(db, thread.candidate_id)
        _copy_linkedin(db, thread)
        _copy_experience(db, thread)
        return thread.candidate_id

    email = (thread.contact_email or "").strip().lower()
    name = (thread.contact_name or "").strip()
    linkedin = canonical_linkedin_url(thread.contact_linkedin) or (thread.contact_linkedin or "").strip()
    if not (email or linkedin):
        # A name is NOT required. An email address identifies somebody on its
        # own, and asking "what name should I put on this?" is a form field,
        # not a conversation -- the name arrives with the LinkedIn profile,
        # which is the whole reason we ask for the URL. A candidate created
        # without one gets their real name the moment the profile lands, via
        # `enrich.promote`.
        #
        # Debug, not warning: the gate can close before the contact questions
        # do, so "not yet" is an ordinary intermediate state. The case that
        # actually matters -- a finished thread with nobody to attach it to --
        # is recorded on the thread by ``_wrap_up``.
        logger.debug(
            "intake thread %s not linkable yet: name=%s email=%s linkedin=%s",
            thread.id,
            bool(name),
            bool(email),
            bool(linkedin),
        )
        return None

    existing = None
    if linkedin:
        existing = owner_of_linkedin(db, org_id=thread.org_id, url=linkedin)
    if not existing and email:
        existing = owner_of_email(db, org_id=thread.org_id, email=email)
    if existing:
        thread.candidate_id = existing
        _mark_crm_tracked(db, existing)
        _copy_linkedin(db, thread)
        _copy_experience(db, thread)
        return existing

    scrape = ((thread.record or {}).get("_linkedin") or {}).get("scrape") or {}
    payload: dict = {
        "source": "intake_thread",
        "source_id": email or linkedin,
        # The address stands in until the profile arrives with a real name.
        # `promote` overwrites it; a blank here would show as an unnamed row
        # in the CRM, which is harder to act on than an address.
        "full_name": name or email or linkedin,
        "linkedin_url": linkedin,
    }
    if email:
        payload["emails"] = [email]
    if scrape.get("current_title"):
        payload["current_title"] = scrape["current_title"]
    if scrape.get("current_employer"):
        payload["current_company"] = scrape["current_employer"]
    if scrape.get("location"):
        payload["location"] = scrape["location"]
    if scrape.get("headline"):
        payload["headline"] = scrape["headline"]
    years = _stated_years(thread)
    if years is not None:
        payload["years_experience"] = years

    result = upsert_candidates(
        db,
        org_id=thread.org_id,
        payloads=[payload],
    )
    if not result.ids:
        return None
    thread.candidate_id = result.ids[0]
    _mark_crm_tracked(db, thread.candidate_id)
    _copy_linkedin(db, thread)
    _copy_experience(db, thread)
    return thread.candidate_id


def _copy_linkedin(db: Session, thread: IntakeThread) -> None:
    """Stamp the URL onto the candidate if they do not already have one."""
    url = canonical_linkedin_url(thread.contact_linkedin) or (thread.contact_linkedin or "").strip()
    if not url or not thread.candidate_id:
        return
    candidate = db.get(Candidate, thread.candidate_id)
    if candidate is None:
        return
    if not (candidate.linkedin_url or "").strip():
        candidate.linkedin_url = url[:400]
        db.flush()


def _stated_years(thread: IntakeThread) -> float | None:
    """Years of experience this thread learned, from the answer or the profile.

    The answer wins: "roughly how many years of experience do you have?" is a
    question we asked, and the scrape's own figure is a sum over dated roles
    that a profile often gets wrong.
    """
    meta = (thread.record or {}).get("_linkedin") or {}
    for value in (meta.get("years"), (meta.get("scrape") or {}).get("experience_years")):
        try:
            years = float(value)
        except (TypeError, ValueError):
            continue
        if years > 0:
            return years
    return None


def _copy_experience(db: Session, thread: IntakeThread) -> None:
    """Write the years onto the candidate when the row still says zero.

    A thread intake creates a candidate with no ``Experience`` rows, so
    ``years_experience`` computes to 0.0 and the matcher scores every posting
    that states a minimum as "0 years of tracked experience" -- a miss on a
    1.2-weight criterion, for somebody who told us the number in the second
    message. Only fills a zero: a resume or a CRM import that counted real
    roles is a better figure than a remembered one.
    """
    if not thread.candidate_id:
        return
    years = _stated_years(thread)
    if years is None:
        return
    candidate = db.get(Candidate, thread.candidate_id)
    if candidate is None or float(candidate.years_experience or 0.0) > 0:
        return
    candidate.years_experience = years
    db.flush()


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
    _mark_crm_tracked(db, candidate.id)

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
