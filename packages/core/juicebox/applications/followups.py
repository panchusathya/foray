"""The one question the apply flow still asks: work authorization.

Thumbs-up is the yes. The package fills what the preference already knows.
Salary and start date are folded in when we have them; they are not surveyed
over text. Voluntary EEO is declined on the form. The only fact we will not
guess — and the only chat question after a card tap — is work authorization.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Application, CandidatePreference, IntakeThread, JobPosting

logger = logging.getLogger(__name__)

#: Blank answer keys worth one chat message. Work authorization is the only
#: fact we will not guess and the only question the apply flow still asks.
#: Salary and start date are filled from the saved preference when we have
#: them; a missing one is left blank, not surveyed over text.
ASKABLE: dict[str, str] = {
    "work_authorization": "any visa sponsorship needed, or are you authorised to work there?",
}

#: Ask order, so the message reads the same way every time.
ASK_ORDER: tuple[str, ...] = ("work_authorization",)

#: Preference keys we fold into the package when the candidate answers later.
#: Broader than ASKABLE: a number they texted still fills salary even though
#: we no longer ask for it.
REFILL_KEYS: tuple[str, ...] = ("work_authorization", "salary_expectation", "notice_period")

#: Blanks that are answered in the portal, not over text. ``why_company`` is
#: normally *drafted* by the package build now (``docgen.answers``); it lands
#: here only when there was no model to draft with, and the message hands it
#: over rather than asking for a paragraph by SMS.
PORTAL_ONLY: dict[str, str] = {
    "why_company": "why {company}",
}


def drafted_for_review(application: Application) -> list[dict]:
    """Answers we drafted that the candidate has not touched yet.

    Named in the follow-up so the draft is read as a draft: an unmentioned
    generated paragraph in somebody's voice is the words-in-your-mouth problem
    in a new place. ``edited`` clears it -- once they have been in the box the
    words are theirs.
    """
    return [
        item
        for item in (application.form_answers or [])
        if item.get("needs_review")
        and str(item.get("answer") or "").strip()
        and not item.get("edited")
    ]


def blank_answers(application: Application) -> list[str]:
    """Answer keys the package could not fill, in ask order."""
    filled = {
        str(item.get("key")): str(item.get("answer") or "").strip()
        for item in (application.form_answers or [])
    }
    return [key for key in ASK_ORDER if key in ASKABLE and not filled.get(key)]


def portal_only_blanks(application: Application) -> list[str]:
    """Blank keys the candidate has to write themselves, in a stable order."""
    filled = {
        str(item.get("key")): str(item.get("answer") or "").strip()
        for item in (application.form_answers or [])
    }
    return [key for key in PORTAL_ONLY if not filled.get(key)]


#: How many of a form's own questions get quoted in the text before the rest
#: collapse to a count. Five custom questions verbatim is a wall of text.
MAX_NAMED_FORM_QUESTIONS = 2


def form_question_blanks(application: Application) -> list[dict]:
    """This form's own unanswered questions, required ones first.

    Discovered from the live form (``applications/questions.py``), and only the
    portal can answer them -- a text reply has nowhere to land, because the
    preference extractor only knows the standard keys.
    """
    blanks = [
        item
        for item in (application.form_answers or [])
        if item.get("kind") == "custom" and not str(item.get("answer") or "").strip()
    ]
    return sorted(blanks, key=lambda item: not item.get("form_required"))


def textable_form_blanks(_application: Application) -> list[dict]:
    """Required custom blanks we used to ask over text. Empty: the apply
    flow asks only work authorization. In-flight rows already tagged
    ``asked_text`` are still absorbed (``awaiting_text_answers``).
    """
    return []


def required_form_blanks(application: Application) -> list[dict]:
    """Only missing work authorization withholds a send.

    Everything else is filled from the package, declined on optional EEO
    fields, or becomes a hand-off inside the submitter. Queueing while a
    custom essay is blank used to be right (the run was guaranteed to
    terminus); asking that essay over chat is the complexity we dropped.
    """
    return [
        item
        for item in (application.form_answers or [])
        if str(item.get("key")) == "work_authorization"
        and not str(item.get("answer") or "").strip()
    ]


def next_text_question(application: Application) -> dict | None:
    """The next required custom question to ask over text, or ``None``.

    ``asked_text`` is the direct-ask flag, distinct from ``asked`` (named once
    in a batch message, historically as "answer in the app"): only a question
    put to the candidate directly may capture their next reply, and legacy
    rows whose ``asked`` was a hand-off must never do that.
    """
    for item in textable_form_blanks(application):
        if not item.get("asked_text"):
            return item
    return None


def mark_text_asked(db: Session, application: Application, key: str) -> None:
    """Record that one form question went out as a direct text question."""
    answers = [dict(item) for item in (application.form_answers or [])]
    for item in answers:
        if str(item.get("key")) == key:
            item["asked_text"] = True
            item["asked"] = True
    application.form_answers = answers
    db.flush()


def attestation_count(application: Application) -> int:
    """Unanswered self-ID questions. Counted, never quoted: race and disability
    do not belong in an SMS, even as a question."""
    return sum(
        1
        for item in (application.form_answers or [])
        if item.get("kind") == "attestation" and not str(item.get("answer") or "").strip()
    )


def followup_message(application: Application, posting: JobPosting | None) -> str:
    """The one question this package still asks, or "" when work
    authorization is already known. No portal hand-off, no self-ID
    survey, no custom-essay interrogation.
    """
    missing = blank_answers(application)
    if not missing:
        return ""
    where = role_phrase(posting)
    asks = " ".join(ASKABLE[key] for key in missing)
    if where:
        return f"{where} — {asks}"
    return asks


def role_phrase(posting: JobPosting | None) -> str:
    """``the Founding Engineer application at Sourcebot``, or "" with nothing
    to name. Shared so an answer is acknowledged in the same words the question
    was asked in."""
    if posting is None:
        return ""
    from ..presentation.jobcard import clean_title

    company = (posting.company or "").strip()
    title = clean_title((posting.title or "").strip(), company)
    if company and title:
        return f"the {title} application at {company}"
    if company:
        return f"the application at {company}"
    if title:
        return f"the {title} application"
    return ""


def awaiting_text_answers(
    db: Session, *, org_id: str, candidate_id: str
) -> Application | None:
    """The application whose texted questions are still unanswered.

    This is the missing half of ``ask_for_missing``. That function asks, marks
    the rows it asked about, and then nothing on the messaging side ever looked
    again -- so a candidate who answered got the generic "noted, thanks" and
    the application they were answering *for* stayed blank until they happened
    to open the portal, which is the only place ``refill_from_preference`` was
    ever called from.

    Only applications that actually asked count (``asked`` on the row). A
    package that filled everything from the preference asked nothing and must
    not capture the next unrelated thing the candidate says.

    Newest first: if two applications are somehow both waiting, the one asked
    about most recently is the one being answered.
    """
    if not candidate_id:
        return None
    rows = db.scalars(
        select(Application)
        .where(
            Application.org_id == org_id,
            Application.candidate_id == candidate_id,
            Application.status.in_(("draft", "prepared")),
        )
        .order_by(Application.updated_at.desc())
    ).all()
    for application in rows:
        answers = application.form_answers or []
        for item in answers:
            key = str(item.get("key") or "")
            blank = not str(item.get("answer") or "").strip()
            if key in ASKABLE and item.get("asked") and blank:
                return application
            # A form question put to them directly (``asked_text``, never the
            # legacy batch ``asked``, which was a portal hand-off).
            if item.get("kind") == "custom" and item.get("asked_text") and blank:
                return application
    return None


def asked_blanks(application: Application) -> list[str]:
    """Keys we asked this candidate for over text and still do not have.

    Narrower than ``blank_answers`` on purpose, and the difference is not
    cosmetic. ``blank_answers`` walks ``ASK_ORDER`` and reads a key that is
    *absent* from ``form_answers`` as blank, which is right for deciding what a
    package still owes -- ``build_form_answers`` writes all four, so an absent
    key there means something went wrong.

    It is wrong for deciding whether a conversation is finished. A key nobody
    was ever asked about is not outstanding from the candidate's point of view,
    and counting it would mean the answer set never reads as complete: the
    acknowledgement would forever say "one more thing" and the submission
    retry would never fire.
    """
    return [
        str(item.get("key"))
        for item in (application.form_answers or [])
        if str(item.get("key")) in ASKABLE
        and item.get("asked")
        and not str(item.get("answer") or "").strip()
    ]


def absorb_text_answers(
    db: Session, application: Application, *, reply_body: str = "", asked_key: str = ""
) -> dict:
    """Fold what the candidate just said into this application, and report.

    Returns ``{"filled": [...], "missing": [...], "handover": bool,
    "next_question": dict | None}``. ``filled`` is what this turn resolved,
    which is what makes the difference between confirming an answer and
    admitting we did not understand it -- the two replies a bare "noted,
    thanks" was collapsing into one.

    ``asked_key`` is ``thread.last_field_asked``: when it names a form
    question we asked directly, the reply body is the answer, written onto the
    row **verbatim** -- never through the preference extractor, which knows
    nothing about ``q_*`` keys. Their words, same standing as a portal PATCH,
    so ``edited`` shields them from re-discovery exactly the same way.
    """
    filled = refill_from_preference(db, application)
    if asked_key and reply_body.strip():
        answers = [dict(item) for item in (application.form_answers or [])]
        for item in answers:
            if (
                str(item.get("key")) == asked_key
                and item.get("kind") == "custom"
                and item.get("asked_text")
                and not str(item.get("answer") or "").strip()
            ):
                item["answer"] = reply_body.strip()
                item["provenance"] = "candidate:text"
                item["edited"] = True
                application.form_answers = answers
                db.flush()
                filled = [*filled, asked_key]
                break
    return {
        "filled": filled,
        "missing": asked_blanks(application),
        "handover": False,
        "next_question": next_text_question(application),
    }


def answer_acknowledgement(
    application: Application, posting: JobPosting | None, outcome: dict
) -> str:
    """What to say back once an answer has been folded in.

    Four cases, and the point of separating them is that three of them were
    previously the same sentence -- "noted, thanks. anything else you want me
    looking for?" -- which named no role, described nothing that had happened,
    and invited a fresh job search on top of an application that was still
    waiting on the candidate.
    """
    where = role_phrase(posting) or "your application"
    filled = outcome.get("filled") or []
    missing = outcome.get("missing") or []

    if missing and not filled:
        # We did not understand it. Re-asking the specific thing is honest;
        # "noted, thanks" claims an answer landed when none did.
        asks = " ".join(ASKABLE[key] for key in missing)
        return f"sorry, i didn't catch that for {where} — {asks}"

    if missing:
        asks = " ".join(ASKABLE[key] for key in missing)
        return f"got it, thanks. one more for {where}: {asks}"

    next_question = outcome.get("next_question")
    if next_question is not None:
        # Their inbound earns one outbound; the acknowledgement and the next
        # form question share it rather than spending two.
        question = str(next_question.get("question") or "").strip().rstrip("?")
        return f'got it. their form also asks: "{question}?"'

    return f"perfect, that's {where} complete. i'll take it from here."


def custom_question_message(
    application: Application, posting: JobPosting | None, item: dict
) -> str:
    """One form question, quoted, named to its role."""
    where = role_phrase(posting)
    question = str(item.get("question") or "").strip().rstrip("?")
    head = (
        f"{where} has one more question on the form"
        if where
        else "one more question on the application form"
    )
    return f'{head}: "{question}?"'


def thread_for(db: Session, application: Application) -> IntakeThread | None:
    """The standing conversation with this candidate, if there is one.

    Newest first: somebody who texted after a web intake is reachable on the
    text thread, and that is where the package link went too.
    """
    from ..messaging.manager import MANAGER_MODES

    threads = db.scalars(
        select(IntakeThread)
        .where(
            IntakeThread.org_id == application.org_id,
            IntakeThread.candidate_id == application.candidate_id,
        )
        .order_by(IntakeThread.updated_at.desc())
    ).all()
    for thread in threads:
        if thread.status == "stopped":
            continue
        if (thread.mode or "") in MANAGER_MODES:
            return thread
    return None


def ask_for_missing(db: Session, application: Application) -> dict:
    """Send the one follow-up this package needs. Idempotent per application.

    Silent when work authorization is already known, nobody is there to ask,
    or we already asked. The apply flow does not survey salary, start date,
    custom essays, or self-ID over chat.
    """
    missing = blank_answers(application)
    if not missing:
        return {"asked": [], "skipped": "package answered everything"}

    pending = [
        item
        for item in (application.form_answers or [])
        if item.get("kind") == "custom"
        and item.get("asked_text")
        and not str(item.get("answer") or "").strip()
    ]
    if pending:
        return {"asked": [], "skipped": "waiting on a texted answer"}

    answers = [dict(item) for item in (application.form_answers or [])]
    already = {str(i.get("key")) for i in answers if i.get("asked")}
    if set(missing) <= already:
        return {"asked": [], "skipped": "already asked"}

    thread = thread_for(db, application)
    if thread is None:
        return {"asked": [], "skipped": "no standing thread"}

    posting = db.get(JobPosting, application.job_posting_id)

    from ..messaging.channels import OutboundMessage
    from ..messaging.conversation import deliver

    body = followup_message(application, posting)
    if not body:
        return {"asked": [], "skipped": "nothing to say"}

    row = deliver(db, thread=thread, message=OutboundMessage(body=body))
    if row is not None:
        for item in answers:
            if str(item.get("key")) in set(missing):
                item["asked"] = True
        application.form_answers = answers
        db.flush()
    return {
        "asked": missing,
        "handed_off": [],
        "sent": 1 if row is not None else 0,
    }


def refill_from_preference(db: Session, application: Application) -> list[str]:
    """Fold answers that have arrived since the build into the answer set.

    The candidate replies to the follow-up, ``_reread`` puts it in the record
    and ``persist_preference`` writes the row -- but the package was rendered
    before any of that, so the form answers still read blank. Called when the
    portal serves the application, so what the candidate sees is what we know
    now rather than what we knew at build time.
    """
    preference = db.scalar(
        select(CandidatePreference).where(
            CandidatePreference.org_id == application.org_id,
            CandidatePreference.candidate_id == application.candidate_id,
        )
    )
    if preference is None:
        return []

    from ..docgen.answers import _deterministic_answers

    company = ""
    posting = db.get(JobPosting, application.job_posting_id)
    if posting is not None:
        company = posting.company or ""
    fresh = {
        item["key"]: item
        for item in _deterministic_answers(preference, company=company or "the company")
    }

    answers = [dict(item) for item in (application.form_answers or [])]
    filled: list[str] = []
    for item in answers:
        key = str(item.get("key"))
        if key not in REFILL_KEYS or str(item.get("answer") or "").strip():
            continue
        # An answer a human edited is theirs, blank or not.
        if item.get("edited"):
            continue
        replacement = fresh.get(key) or {}
        answer = str(replacement.get("answer") or "").strip()
        if not answer:
            continue
        item["answer"] = answer
        item["provenance"] = replacement.get("provenance") or item.get("provenance") or "none"
        item["generated_by"] = "template"
        filled.append(key)

    if filled:
        application.form_answers = answers
        db.flush()
    return filled
