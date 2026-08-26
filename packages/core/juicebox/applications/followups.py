"""The questions an application needs, asked when an application needs them.

Onboarding used to close with a batch of three — years, comp, timing — before
the candidate had been shown a single role. Two of those are literally in
``docgen.answers.STANDARD_QUESTIONS``: an employer asks for a salary
expectation and a notice period on the form, and we asked them weeks earlier,
in the abstract, from somebody who had no idea what they were being considered
for. The answer went stale and the conversation spent its best messages on a
survey.

So they move here. A package is built, the answer set comes back with blanks,
and the blanks are what we ask about — in one message, naming the role, at the
moment the number actually decides something. "Google wants a salary
expectation on the form" is a question somebody can answer. "What are you
targeting on comp?" three weeks earlier is a form field.

**Only ever blanks.** ``build_form_answers`` fills what the preference already
knows, so a candidate who volunteered a number is never asked for it again --
and the extractor absorbs whatever comes back on the next turn, so the second
application does not ask a third time.

``why_company`` arrives *drafted* now (``answers.py``: from the resume and
the posting, flagged ``needs_review``), so the follow-up hands the draft over
for review rather than asking for a paragraph by SMS. A blank one -- no model
to draft with -- is still named and pointed at the portal: an empty "why this
company" the text never mentions reads as a question the agent missed rather
than one it deliberately left.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Application, CandidatePreference, IntakeThread, JobPosting

logger = logging.getLogger(__name__)

#: Blank answer keys worth one message, and the field each one lands in when
#: the reply is re-extracted. ``why_company`` is absent on purpose.
ASKABLE: dict[str, str] = {
    "salary_expectation": "what should we put for comp expectations?",
    "notice_period": "when could you start?",
    "work_authorization": "any visa sponsorship needed, or are you authorised to work there?",
}

#: Ask order, so the message reads the same way every time.
ASK_ORDER: tuple[str, ...] = ("salary_expectation", "notice_period", "work_authorization")

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


def attestation_count(application: Application) -> int:
    """Unanswered self-ID questions. Counted, never quoted: race and disability
    do not belong in an SMS, even as a question."""
    return sum(
        1
        for item in (application.form_answers or [])
        if item.get("kind") == "attestation" and not str(item.get("answer") or "").strip()
    )


def followup_message(application: Application, posting: JobPosting | None) -> str:
    """One message naming the role and the blanks, or "" when nothing is owed."""
    missing = blank_answers(application)
    mine = portal_only_blanks(application)
    drafted = drafted_for_review(application)
    form_blanks = form_question_blanks(application)
    attestations = attestation_count(application)
    if not missing and not mine and not drafted and not form_blanks and not attestations:
        return ""

    where = role_phrase(posting)
    head = (
        f"{where} asks a couple of things I don't have yet:"
        if where
        else "the application form asks a couple of things I don't have yet:"
    )
    lines = [f"- {ASKABLE[key]}" for key in missing]
    company = ((posting.company if posting else "") or "the company").strip()
    for key in mine:
        lines.append(
            f"- {PORTAL_ONLY[key].format(company=company)} — "
            "write it in the app and i'll include it."
        )
    for _item in drafted:
        lines.append(
            f"- i drafted why {company} for you from your background — "
            "read it in the app and change anything before it goes out."
        )
    for item in form_blanks[:MAX_NAMED_FORM_QUESTIONS]:
        question = str(item.get("question") or "").strip().rstrip("?")
        lines.append(
            f'- their form asks: "{question}?" '
            "— answer in the app and i'll include it."
        )
    overflow = len(form_blanks) - MAX_NAMED_FORM_QUESTIONS
    if overflow > 0:
        lines.append(f"- plus {overflow} more of their questions, all in the app.")
    if attestations:
        plural = "questions" if attestations > 1 else "question"
        lines.append(
            f"- {attestations} self-ID {plural} (things like veteran or disability "
            "status) — in the app too. only you can answer those; i never fill them in."
        )
    if not missing:
        # Nothing to ask, only things to hand over. A question head would
        # misdescribe what this message wants.
        head = (
            f"{where} needs a few things from you:"
            if where
            else "your application needs a few things from you:"
        )
    return head + "\n\n" + "\n".join(lines)


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
            if (
                key in ASKABLE
                and item.get("asked")
                and not str(item.get("answer") or "").strip()
            ):
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


def absorb_text_answers(db: Session, application: Application) -> dict:
    """Fold what the candidate just said into this application, and report.

    Returns ``{"filled": [...], "missing": [...], "handover": bool}``.
    ``filled`` is what this turn resolved, which is what makes the difference
    between confirming an answer and admitting we did not understand it -- the
    two replies a bare "noted, thanks" was collapsing into one.
    """
    filled = refill_from_preference(db, application)
    return {
        "filled": filled,
        "missing": asked_blanks(application),
        "handover": bool(
            portal_only_blanks(application)
            or form_question_blanks(application)
            or attestation_count(application)
        ),
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

    if outcome.get("handover"):
        # Everything answerable over text is in. What is left is deliberately
        # not askable here -- their own words, the form's own questions, and
        # self-ID -- so this says where it lives rather than asking again.
        return (
            f"that's everything i needed for {where}. the last few bits are "
            "yours to fill in — they're waiting in the app."
        )
    return f"perfect, that's {where} complete. i'll take it from here."


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

    Silent when there is nothing to ask, nobody to ask, or we already asked for
    this application — a second copy of the same three questions is the thing
    that made the agent look like it was not listening.
    """
    missing = blank_answers(application)
    # Everything the message hands over rather than asks: the write-it-yourself
    # keys, drafts awaiting the candidate's eyes, this form's own questions,
    # and the self-ID entries. The self-ID ones are named here only so the
    # ask-once flag lands on their rows -- the rendered message carries them
    # as a count, never by name.
    mine = (
        portal_only_blanks(application)
        + [str(item.get("key")) for item in drafted_for_review(application)]
        + [
            str(item.get("key"))
            for item in (application.form_answers or [])
            if item.get("kind") in ("custom", "attestation")
            and not str(item.get("answer") or "").strip()
        ]
    )
    if not missing and not mine:
        return {"asked": [], "skipped": "package answered everything"}

    # "Have we asked?" is recorded on the answer rows themselves rather than a
    # new column: they are already JSON on this application, and a flag that
    # travels with the answer cannot drift from it.
    answers = [dict(item) for item in (application.form_answers or [])]
    already = {str(i.get("key")) for i in answers if i.get("asked")}
    if set(missing) | set(mine) <= already:
        return {"asked": [], "skipped": "already asked"}

    thread = thread_for(db, application)
    if thread is None:
        return {"asked": [], "skipped": "no standing thread"}

    posting = db.get(JobPosting, application.job_posting_id)
    body = followup_message(application, posting)
    if not body:
        return {"asked": [], "skipped": "nothing to say"}

    from ..messaging.channels import OutboundMessage
    from ..messaging.conversation import deliver

    row = deliver(db, thread=thread, message=OutboundMessage(body=body))
    for item in answers:
        if str(item.get("key")) in set(missing) | set(mine):
            item["asked"] = True
    application.form_answers = answers
    db.flush()
    # `asked` stays the questions we actually asked. A portal-only blank was
    # named, not asked, and a caller counting questions should not see it --
    # but the row still carries `asked` so it is not named twice.
    return {
        "asked": missing,
        "handed_off": mine,
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
        if key not in ASKABLE or str(item.get("answer") or "").strip():
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
