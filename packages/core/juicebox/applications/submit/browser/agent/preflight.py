"""Attempt-free question discovery. Never submits, never writes an attempt row."""

from __future__ import annotations

import logging

from ...base import SubmissionContext
from ..questions import (
    KIND_ATTESTATION,
    KIND_CUSTOM,
    KIND_IDENTITY,
    KIND_STANDARD,
    FormQuestion,
    classify,
    question_key,
)
from . import loop
from .observe import Control

logger = logging.getLogger(__name__)

ORIGIN_FORM = "form"


def discover_questions(ctx: SubmissionContext) -> list[FormQuestion]:
    """Walk the live form with submit disabled and return what it asks.

    Failures degrade to an empty list: the standard answer set is a complete
    package, and a slow page must not block building one.
    """
    try:
        result = loop.run(ctx, allow_submit=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent preflight failed for %s: %s", ctx.application_id, exc)
        return []
    return questions_from_controls(result.questions)


def questions_from_controls(controls: list[Control]) -> list[FormQuestion]:
    found: list[FormQuestion] = []
    seen: set[str] = set()
    for control in controls:
        if control.field_type in {"password", "hidden"}:
            continue
        if control.role in {"button", "submit", "link"}:
            continue
        label = (control.label or "").strip()
        if not label:
            continue
        kind, standard_key = classify(label)
        if kind == KIND_IDENTITY:
            key = standard_key or control.id
        elif kind == KIND_STANDARD:
            key = standard_key
        else:
            key = question_key(label)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(
            FormQuestion(
                key=key,
                label=label[:200],
                kind=kind,
                field_type=_field_type(control),
                selector=f'[data-juicebox-id="{control.id}"]',
                required=bool(control.required),
                options=tuple(control.options),
                standard_key=standard_key,
            )
        )
    return found


def merge_discovered(answers: list[dict], discovered: list[FormQuestion]) -> list[dict]:
    """Fold preflight questions into the package's answer set.

    Existing answers win; DOM facts (required, options, selector) refresh.
    """
    merged = [dict(item) for item in answers]
    existing = {str(item.get("key")): item for item in merged}
    for question in discovered:
        if question.kind not in (KIND_CUSTOM, KIND_ATTESTATION):
            if question.standard_key in existing and question.required:
                existing[question.standard_key]["form_required"] = True
            continue
        held = existing.get(question.key)
        if held is not None:
            held.update(
                selector=question.selector,
                field_type=question.field_type,
                options=list(question.options),
                form_required=question.required,
            )
            if question.kind == KIND_ATTESTATION and not str(held.get("answer") or "").strip():
                from ....eeo import decline_choice

                choice = decline_choice(question.options)
                if choice:
                    held["answer"] = choice
                    held["provenance"] = "policy:decline"
                    held["generated_by"] = "policy"
            continue
        from ....eeo import decline_choice

        choice = (
            decline_choice(question.options) if question.kind == KIND_ATTESTATION else ""
        )
        merged.append(
            {
                "key": question.key,
                "question": question.label,
                "answer": choice,
                "provenance": "policy:decline" if choice else "none",
                "generated_by": "policy" if choice else "form",
                "edited": False,
                "origin": ORIGIN_FORM,
                "kind": question.kind,
                "field_type": question.field_type,
                "selector": question.selector,
                "options": list(question.options),
                "form_required": question.required,
            }
        )
    return merged


def _field_type(control: Control) -> str:
    mapping = {
        "email": "text",
        "tel": "text",
        "url": "text",
        "text": "text",
        "textarea": "textarea",
        "select": "select",
        "radio": "radio",
        "checkbox": "checkbox",
        "file": "file",
    }
    return mapping.get(control.field_type, "text")
