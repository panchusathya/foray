"""``resume.v1`` JSON → PDF bytes, and a letter to match.

reportlab platypus: real pagination for a two-page history, no HTML engine,
no system packages. One clean template on purpose — the differentiator is the
content pipeline, not a theme gallery, and every escaping bug lives in the
one place ``_escape`` is."""

from __future__ import annotations

import io
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

_MARGIN = 0.75 * inch

_NAME = ParagraphStyle(
    "name", fontName="Helvetica-Bold", fontSize=17, leading=21, alignment=TA_LEFT
)
_CONTACT = ParagraphStyle("contact", fontName="Helvetica", fontSize=9, leading=12, textColor="#444444")
_HEADING = ParagraphStyle(
    "heading", fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=10, spaceAfter=2
)
_ROLE = ParagraphStyle("role", fontName="Helvetica-Bold", fontSize=10, leading=13, spaceBefore=6)
_DATES = ParagraphStyle("dates", fontName="Helvetica-Oblique", fontSize=9, leading=12, textColor="#555555")
_BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=13)
_BULLET = ParagraphStyle("bullet", parent=_BODY, leftIndent=0)


def _escape(value: object) -> str:
    return _xml_escape(str(value or "").strip())


def _rule() -> HRFlowable:
    return HRFlowable(width="100%", thickness=0.6, color="#999999", spaceAfter=4)


def render_resume_pdf(structured: dict) -> bytes:
    """One resume, letter-size, from a ``resume.v1`` dict."""
    contact = structured.get("contact") or {}
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN,
        bottomMargin=_MARGIN,
        title=f"{contact.get('name') or 'Resume'} - Resume",
    )
    story: list = []

    if contact.get("name"):
        story.append(Paragraph(_escape(contact["name"]), _NAME))
    contact_bits = [
        _escape(contact.get(key))
        for key in ("email", "phone", "location", "linkedin_url", "github_url", "website_url")
        if contact.get(key)
    ]
    if contact_bits:
        story.append(Paragraph(" | ".join(contact_bits), _CONTACT))
    story.append(Spacer(1, 6))

    summary = (structured.get("summary") or "").strip()
    if summary:
        story.append(Paragraph("SUMMARY", _HEADING))
        story.append(_rule())
        story.append(Paragraph(_escape(summary), _BODY))

    experience = structured.get("experience") or []
    if experience:
        story.append(Paragraph("EXPERIENCE", _HEADING))
        story.append(_rule())
        for job in experience:
            if not isinstance(job, dict):
                continue
            head = " — ".join(x for x in (_escape(job.get("title")), _escape(job.get("company"))) if x)
            if head:
                story.append(Paragraph(head, _ROLE))
            span = " - ".join(x for x in (_escape(job.get("start")), _escape(job.get("end"))) if x)
            if span:
                story.append(Paragraph(span, _DATES))
            bullets = [b for b in (job.get("bullets") or []) if str(b).strip()]
            if bullets:
                story.append(
                    ListFlowable(
                        [
                            ListItem(Paragraph(_escape(b), _BULLET), leftIndent=12)
                            for b in bullets
                        ],
                        bulletType="bullet",
                        start="•",
                        leftIndent=12,
                    )
                )

    education = structured.get("education") or []
    if education:
        story.append(Paragraph("EDUCATION", _HEADING))
        story.append(_rule())
        for edu in education:
            if not isinstance(edu, dict):
                continue
            line = " — ".join(
                x
                for x in (
                    _escape(edu.get("school")),
                    ", ".join(
                        y for y in (_escape(edu.get("degree")), _escape(edu.get("field"))) if y
                    ),
                )
                if x
            )
            years = "-".join(
                x for x in (_escape(edu.get("start_year")), _escape(edu.get("end_year"))) if x
            )
            if years:
                line = f"{line} ({years})" if line else years
            if line:
                story.append(Paragraph(line, _BODY))

    skills = [str(s).strip() for s in (structured.get("skills") or []) if str(s).strip()]
    if skills:
        story.append(Paragraph("SKILLS", _HEADING))
        story.append(_rule())
        story.append(Paragraph(_escape(", ".join(skills)), _BODY))

    certifications = [
        str(c).strip() for c in (structured.get("certifications") or []) if str(c).strip()
    ]
    if certifications:
        story.append(Paragraph("CERTIFICATIONS", _HEADING))
        story.append(_rule())
        for cert in certifications:
            story.append(Paragraph(_escape(cert), _BODY))

    languages = [str(x).strip() for x in (structured.get("languages") or []) if str(x).strip()]
    if languages:
        story.append(Paragraph("LANGUAGES", _HEADING))
        story.append(_rule())
        story.append(Paragraph(_escape(", ".join(languages)), _BODY))

    doc.build(story)
    return buffer.getvalue()


def render_letter_pdf(body_text: str, *, candidate_name: str = "", title: str = "") -> bytes:
    """A plain letter: the cover-letter body, paragraph per blank line."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN,
        bottomMargin=_MARGIN,
        title=title or (f"{candidate_name} - Cover Letter".strip(" -")),
    )
    story: list = []
    for paragraph in (body_text or "").split("\n\n"):
        text = paragraph.strip()
        if not text:
            continue
        story.append(Paragraph(_escape(text).replace("\n", "<br/>"), _BODY))
        story.append(Spacer(1, 10))
    doc.build(story)
    return buffer.getvalue()
