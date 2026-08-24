"""Uploaded resume PDF → text → ``resume.v1`` JSON.

Extraction is supply-side fact, not preference, so this is a sibling of
``intake/extract.py`` rather than a caller of it. Same posture though: strict
schema on the model path, and it must work — degraded, honestly — with no
``ANTHROPIC_API_KEY``.

There is deliberately no heuristic resume parser. A regex over resume prose
produces confidently wrong employment history, and wrong beats empty in no
world where the output is asserted under somebody's name. No model ⇒ the text
is kept, ``structured`` stays empty, and the row says why.
"""

from __future__ import annotations

import io
import logging

from ..llm.client import LLMUnavailable, get_llm
from ..llm.prompts import RESUME_PARSE_SCHEMA, RESUME_PARSE_SYSTEM, resume_parse_prompt

logger = logging.getLogger(__name__)

# Uploads are capped at settings.max_upload_bytes; a real resume is 1-3 pages.
MAX_PDF_PAGES = 20

ERROR_NO_TEXT = "no_text (scanned image? OCR is out of scope)"
ERROR_LLM_UNAVAILABLE = "llm_unavailable (text kept; parse re-runs when a model is configured)"


def extract_pdf_text(data: bytes) -> str:
    """Page-joined text via pypdf. Any failure is an empty string, not a raise:
    the caller records *why* on the row, and a corrupt PDF is a data point,
    not a worker crash."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = reader.pages[:MAX_PDF_PAGES]
        chunks = []
        for page in pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page must not lose the rest
                continue
        return "\n".join(chunk for chunk in chunks if chunk.strip()).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf text extraction failed: %s", exc)
        return ""


def parse_resume_text(text: str) -> tuple[dict, str]:
    """``(resume.v1 dict, error)``. Exactly one of the two is truthy."""
    if not (text or "").strip():
        return {}, ERROR_NO_TEXT
    llm = get_llm()
    if not llm.available:
        return {}, ERROR_LLM_UNAVAILABLE
    try:
        result = llm.json(
            system=RESUME_PARSE_SYSTEM,
            prompt=resume_parse_prompt(text=text),
            schema=RESUME_PARSE_SCHEMA,
        )
    except LLMUnavailable:
        return {}, ERROR_LLM_UNAVAILABLE
    except Exception as exc:  # noqa: BLE001 - recorded on the row, job retries
        logger.warning("resume parse failed: %s", exc)
        return {}, f"parse_failed: {str(exc)[:200]}"
    data = result.data or {}
    if not isinstance(data, dict) or (not data.get("experience") and not data.get("contact")):
        return {}, "parse_empty (text did not read as a resume)"
    return data, ""
