"""Uploaded resume (PDF or DOCX) → text → ``resume.v1`` JSON.

Extraction is supply-side fact, not preference, so this is a sibling of
``intake/extract.py`` rather than a caller of it. Same posture though: strict
schema on the model path, and it must work — degraded, honestly — with no
``ANTHROPIC_API_KEY``.

There is deliberately no heuristic resume parser. A regex over resume prose
produces confidently wrong employment history, and wrong beats empty in no
world where the output is asserted under somebody's name. No model ⇒ the text
is kept, ``structured`` stays empty, and the row says why.

Type is decided by magic bytes, never by filename or a client Content-Type
header (``sniff_resume_type``). The two types accepted are the two formats
resumes actually come in; ``.doc`` (the 1997 binary format) is refused
honestly rather than parsed badly, and OCR on scanned images stays out of
scope.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from xml.etree import ElementTree

from ..llm.client import LLMUnavailable, get_llm
from ..llm.prompts import RESUME_PARSE_SCHEMA, RESUME_PARSE_SYSTEM, resume_parse_prompt

logger = logging.getLogger(__name__)

# Uploads are capped at settings.max_upload_bytes; a real resume is 1-3 pages.
MAX_PDF_PAGES = 20

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: Suffix for the id-built storage path, keyed by sniffed type.
SUFFIX_BY_MIME = {PDF_MIME: ".pdf", DOCX_MIME: ".docx"}

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"

ERROR_NO_TEXT = "no_text (scanned image? OCR is out of scope)"
ERROR_LLM_UNAVAILABLE = "llm_unavailable (text kept; parse re-runs when a model is configured)"

# Resume parsing is a background task, but it gates an application run.  A
# normal LLM call can wait and retry for several minutes, leaving the document
# permanently "parsing" and stranding the browser worker behind it.  Keep the
# structured parse bounded; the original file and extracted text remain on the
# protected document row if the model is temporarily unavailable.
RESUME_PARSE_TIMEOUT_SECONDS = 30.0


def sniff_resume_type(data: bytes) -> str:
    """The MIME type this file actually is: PDF, DOCX, or ``""``.

    A DOCX is a zip that contains ``word/document.xml`` — the content check
    matters because xlsx/pptx (and any random zip) share the ``PK`` magic,
    and "we accepted your resume" must not be said about a spreadsheet.
    """
    if data.startswith(_PDF_MAGIC):
        return PDF_MIME
    if data.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "word/document.xml" in archive.namelist():
                    return DOCX_MIME
        except Exception:  # noqa: BLE001 - a truncated zip is just "not a docx"
            return ""
    return ""


def extract_text(data: bytes) -> str:
    """Text from an uploaded resume, whichever accepted type it is."""
    kind = sniff_resume_type(data)
    if kind == PDF_MIME:
        return extract_pdf_text(data)
    if kind == DOCX_MIME:
        return extract_docx_text(data)
    return ""


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


_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_PART = re.compile(r"^word/(document|header\d*|footer\d*)\.xml$")


def extract_docx_text(data: bytes) -> str:
    """Paragraph-joined text from a DOCX, stdlib only.

    The body first, then headers and footers — resumes routinely put the name
    and contact line in a header, and losing that loses the person. Tables
    flatten naturally because every cell's text lives in ordinary ``w:p``
    paragraphs. Same failure posture as the PDF path: any problem is an empty
    string the caller records, never a raise.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if _DOCX_PART.match(n)]
            names.sort(key=lambda n: (0 if n == "word/document.xml" else 1, n))
            chunks: list[str] = []
            for name in names:
                try:
                    chunks.append(_docx_part_text(archive.read(name)))
                except Exception:  # noqa: BLE001 - one bad part must not lose the rest
                    continue
        return "\n".join(chunk for chunk in chunks if chunk.strip()).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("docx text extraction failed: %s", exc)
        return ""


def _docx_part_text(xml: bytes) -> str:
    root = ElementTree.fromstring(xml)
    paragraphs: list[str] = []
    for para in root.iter(f"{_WORD_NS}p"):
        runs: list[str] = []
        for node in para.iter():
            if node.tag == f"{_WORD_NS}t":
                runs.append(node.text or "")
            elif node.tag == f"{_WORD_NS}tab":
                runs.append("\t")
            elif node.tag == f"{_WORD_NS}br":
                runs.append("\n")
        text = "".join(runs)
        if text.strip():
            paragraphs.append(text)
    return "\n".join(paragraphs)


def parse_resume_text(text: str) -> tuple[dict, str]:
    """``(resume.v1 dict, error)``. Exactly one of the two is truthy."""
    if not (text or "").strip():
        return {}, ERROR_NO_TEXT
    llm = get_llm()
    if not llm.available:
        return {}, ERROR_LLM_UNAVAILABLE
    try:
        result = llm.json_fast(
            system=RESUME_PARSE_SYSTEM,
            prompt=resume_parse_prompt(text=text),
            schema=RESUME_PARSE_SCHEMA,
            max_tokens=8000,
            timeout=RESUME_PARSE_TIMEOUT_SECONDS,
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
