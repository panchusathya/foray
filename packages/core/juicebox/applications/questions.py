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
    from .submit.browser.cloud import agent_configured

    target = detect.target_of(posting)
    if not agent_configured():
        logger.info(
            "form questions skipped for %s: browser agent is not configured",
            application.id,
        )
        return answers, "skipped: browser agent is not configured"
    url = (posting.apply_url or posting.source_url or target.apply_url or "").strip()
    if not url.startswith(("http://", "https://")):
        logger.info("form questions skipped for %s: no apply target", application.id)
        return answers, "skipped: no apply target"
    if target.vendor in {
        detect.VENDOR_WORKDAY,
        detect.VENDOR_ORACLE,
        detect.VENDOR_YC,
    }:
        logger.info(
            "form questions skipped for %s: vendor %r is account-gated",
            application.id,
            target.vendor,
        )
        return answers, f"skipped: vendor {target.vendor} is account-gated"

    from .submit.base import SubmissionContext
    from .submit.browser.agent.preflight import discover_questions, merge_discovered

    ctx = SubmissionContext(
        application_id=application.id,
        org_id=application.org_id,
        candidate_id=application.candidate_id,
        target=target or detect.ApplyTarget(apply_url=url, tier=detect.TIER_BROWSER),
        answers=list(answers),
        posting_url=url,
    )
    try:
        discovered = discover_questions(ctx)
    except Exception as exc:  # noqa: BLE001 - the standard set is a complete package
        logger.warning("form discovery failed for %s: %s", application.id, exc)
        return answers, f"skipped: discovery failed ({exc})"
    merged = merge_discovered(answers, discovered)
    added = max(0, len(merged) - len(answers))
    if added:
        logger.info(
            "form added %d question(s) to application %s (%s)",
            added,
            application.id,
            target.vendor or "public",
        )
    return merged, "ran"


def unanswerable_questions(ctx) -> list[str]:
    """Labels of required discovered questions nobody has answered.

    Checked before the browser ever opens: a required custom or attestation
    with no approved answer parks the run. An optional attestation left blank
    IS an answer (declining to self-identify).
    """
    labels: list[str] = []
    for entry in getattr(ctx, "answers", None) or []:
        if entry.get("kind") not in ("custom", "attestation"):
            continue
        if str(entry.get("answer") or "").strip():
            continue
        if entry.get("form_required"):
            labels.append(str(entry.get("question") or entry.get("key")))
    return labels


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
