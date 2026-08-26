"""Fold a form's own questions into the application's answer set.

``docgen/answers`` builds the four standard answers; ``submit/browser/questions``
reads what one specific form actually asks. This module joins them at package
time, so the answer set the candidate reviews in the portal is the real form,
not our guess at a generic one.

Discovery is strictly best-effort. It needs a browser, a network fetch and a
vendor whose form we can reach — any of which can be absent — and a package
that fails to build because a page was slow would be backwards: the standard
set is what we shipped for months and remains a complete fallback. Failure
here logs and returns the answers unchanged.

The merge is by key, and keys are derived from the question's own label
(``question_key``), so rebuilding a package after the candidate answered in
the portal keeps their words: an existing entry's answer always wins over a
freshly discovered blank.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import Application, JobPosting

logger = logging.getLogger(__name__)

#: form_answers entries carry `origin` so every surface can tell the four
#: standard questions from the ones a specific form added.
ORIGIN_STANDARD = "standard"
ORIGIN_FORM = "form"


def augment_with_form_questions(
    db: Session,
    *,
    application: Application,
    posting: JobPosting,
    answers: list[dict],
) -> tuple[list[dict], str]:
    """The standard answers plus whatever this form actually asks.

    Returns ``(answers, outcome)``; the caller assigns the list to
    ``application.form_answers``. ``outcome`` is ``"ran"`` or
    ``"skipped: <reason>"`` and travels on the package result -- the first
    live package skipped discovery *silently* (the vendor guard below has no
    log), so the portal presented our generic four as the employer's form and
    nothing anywhere said so.
    """
    from .ats import detect
    from .submit.browser import driver
    from .submit.browser.forms import DISCOVERABLE

    target = detect.target_of(posting)
    if not target or not target.apply_url:
        logger.info("form questions skipped for %s: no apply target", application.id)
        return answers, "skipped: no apply target"
    if target.vendor not in DISCOVERABLE:
        # DISCOVERABLE, not PROBEABLE: an account-gated vendor (Workday, YC)
        # is probeable but its logged-out page is a registration form, and
        # reading questions off that would surface "password" as the
        # employer's.
        logger.info(
            "form questions skipped for %s: vendor %r has no reachable form",
            application.id,
            target.vendor,
        )
        return answers, f"skipped: vendor {target.vendor} has no reachable form"
    ok, reason = driver.available()
    if not ok:
        logger.info("form questions skipped for %s: %s", application.id, reason)
        return answers, f"skipped: {reason}"

    from .submit.browser.questions import KIND_ATTESTATION, KIND_CUSTOM, discover

    try:
        discovered = discover(target.apply_url, vendor=target.vendor)
    except Exception as exc:  # noqa: BLE001 - the standard set is a complete package
        logger.warning("form discovery failed for %s: %s", application.id, exc)
        return answers, f"skipped: discovery failed ({exc})"

    merged = [dict(item) for item in answers]
    existing = {str(item.get("key")): item for item in merged}
    added = 0
    for question in discovered:
        if question.kind not in (KIND_CUSTOM, KIND_ATTESTATION):
            # Identity fields the adapter fills; standard ones the set already
            # carries. Either way there is nothing to add — but a standard
            # question the form marks required is worth stamping, so the
            # submit-time required check reflects the form rather than us.
            if question.standard_key in existing and question.required:
                existing[question.standard_key]["form_required"] = True
            continue
        held = existing.get(question.key)
        if held is not None:
            # Re-discovery after an edit: their answer wins, the DOM facts
            # (selector, options, required) refresh to what the form says now.
            held.update(
                selector=question.selector,
                field_type=question.field_type,
                options=list(question.options),
                form_required=question.required,
            )
            continue
        merged.append(
            {
                "key": question.key,
                "question": question.label,
                "answer": "",
                "provenance": "none",
                "generated_by": "form",
                "edited": False,
                "origin": ORIGIN_FORM,
                "kind": question.kind,
                "field_type": question.field_type,
                "selector": question.selector,
                "options": list(question.options),
                "form_required": question.required,
            }
        )
        added += 1
    if added:
        logger.info(
            "form added %d question(s) to application %s (%s)",
            added,
            application.id,
            target.vendor,
        )
    return merged, "ran"


def custom_blanks(application: Application) -> list[dict]:
    """Unanswered form-specific questions a person could answer."""
    from .submit.browser.questions import KIND_CUSTOM

    return [
        item
        for item in (application.form_answers or [])
        if item.get("kind") == KIND_CUSTOM and not str(item.get("answer") or "").strip()
    ]


def attestation_blanks(application: Application) -> list[dict]:
    """Unanswered self-ID questions. Only the candidate's hand fills these."""
    from .submit.browser.questions import KIND_ATTESTATION

    return [
        item
        for item in (application.form_answers or [])
        if item.get("kind") == KIND_ATTESTATION and not str(item.get("answer") or "").strip()
    ]
