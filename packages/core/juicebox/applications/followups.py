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

``why_company`` is never *answered* for somebody -- it is blank by design
(``answers.py``: we do not write their enthusiasm for them) and it is theirs to
fill in on the portal. It is still **named** in the follow-up, which it was
not: the portal showed an empty "why this company" while the text asked only
about comp, so it read as a question the agent had missed rather than one it
had deliberately left.
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

#: Blanks we will not fill and will not ask to be answered over text, because
#: the answer has to be in the candidate's own words. Named in the message
#: anyway: the portal showed an empty "why this company" while the text asked
#: only about comp, which reads as the agent having missed one rather than
#: having left it deliberately.
PORTAL_ONLY: dict[str, str] = {
    "why_company": "why {company}",
}


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


def followup_message(application: Application, posting: JobPosting | None) -> str:
    """One message naming the role and the blanks, or "" when nothing is owed."""
    missing = blank_answers(application)
    mine = portal_only_blanks(application)
    if not missing and not mine:
        return ""

    where = ""
    if posting is not None:
        company = (posting.company or "").strip()
        title = (posting.title or "").strip()
        if company and title:
            where = f"the {title} application at {company}"
        elif company:
            where = f"the application at {company}"
        elif title:
            where = f"the {title} application"
    head = (
        f"{where} asks a couple of things I don't have yet:"
        if where
        else "the application form asks a couple of things I don't have yet:"
    )
    lines = [f"- {ASKABLE[key]}" for key in missing]
    company = ((posting.company if posting else "") or "the company").strip()
    for key in mine:
        lines.append(
            f"- {PORTAL_ONLY[key].format(company=company)} — that one's yours to write, "
            "i won't put words in your mouth. add it in the app and i'll include it."
        )
    if not missing:
        # Nothing to ask, only something to hand over. A question head would
        # misdescribe what this message wants.
        head = (
            f"{where} needs one thing from you:"
            if where
            else "your application needs one thing from you:"
        )
    return head + "\n\n" + "\n".join(lines)


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
    mine = portal_only_blanks(application)
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
