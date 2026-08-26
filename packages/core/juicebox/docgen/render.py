"""``resume.v1`` JSON → PDF bytes, and a letter to match.

reportlab platypus: real pagination for a two-page history, no HTML engine,
no system packages. One clean template on purpose — the differentiator is the
content pipeline, not a theme gallery, and every escaping bug lives in the
one place ``_escape`` is.

The layout follows the classic finance/consulting resume: centered name and
contact line, ruled section headings, two-column entry rows (organization
bold left, location bold right; title italic left, dates italic right), and
a single SKILLS & INTERESTS block of labeled lines. Section ORDER flexes
with the person: professional experience leads, unless the record reads as a
student or brand-new graduate — still in school, or under ~18 months of
history — in which case education leads, the convention every campus career
center teaches. A leadership section appears exactly when the data has one
and takes whatever room its entries need.
"""

from __future__ import annotations

import io
import re
from datetime import date
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
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
    Table,
    TableStyle,
)

_MARGIN = 0.75 * inch
_CONTENT_WIDTH = LETTER[0] - 2 * _MARGIN

_NAME = ParagraphStyle(
    "name", fontName="Helvetica-Bold", fontSize=16, leading=20, alignment=TA_CENTER
)
_CONTACT = ParagraphStyle(
    "contact",
    fontName="Helvetica",
    fontSize=9,
    leading=12,
    textColor="#444444",
    alignment=TA_CENTER,
)
_HEADING = ParagraphStyle(
    "heading", fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=10, spaceAfter=2
)
_ROLE = ParagraphStyle("role", fontName="Helvetica-Bold", fontSize=10, leading=13, alignment=TA_LEFT)
_ROLE_R = ParagraphStyle("role_r", parent=_ROLE, alignment=TA_RIGHT)
_TITLE = ParagraphStyle(
    "title", fontName="Helvetica-Oblique", fontSize=10, leading=13, alignment=TA_LEFT
)
_DATES = ParagraphStyle(
    "dates",
    fontName="Helvetica-Oblique",
    fontSize=9,
    leading=13,
    textColor="#555555",
    alignment=TA_RIGHT,
)
_BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=13)
_BULLET = ParagraphStyle("bullet", parent=_BODY, leftIndent=0)


def _escape(value: object) -> str:
    return _xml_escape(str(value or "").strip())


def _rule() -> HRFlowable:
    return HRFlowable(width="100%", thickness=0.6, color="#999999", spaceAfter=4)


# CRM skills are stored lowercase because the search taxonomy normalizes them
# for matching. That is the right shape for retrieval and the wrong one for a
# page with somebody's name on it — "aws, llm, pytorch" reads as carelessness
# an ATS keyword screen does not forgive. Display casing is repaired HERE,
# render-only, so the stored form never changes: known acronyms uppercase,
# known brands get their trademark casing, everything else gets a capital
# first letter. A word that already carries any uppercase (a parsed upload
# kept the author's own casing) is left exactly as written.
_ACRONYMS = frozenset(
    ["ai", "api", "apis", "aws", "ci", "cd", "cfa", "cpa", "cuda", "etl", "gcp", "gpu", "grpc", "html", "css", "ios", "kpi", "llm", "llms", "ml", "nlp", "php", "qa", "rest", "sdk", "sre", "sql", "nosql", "ui", "ux", "vba"]
)
_BRANDS = {
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "node.js": "Node.js",
    "nodejs": "Node.js",
    "next.js": "Next.js",
    "github": "GitHub",
    "gitlab": "GitLab",
    "postgresql": "PostgreSQL",
    "postgres": "Postgres",
    "mysql": "MySQL",
    "mongodb": "MongoDB",
    "graphql": "GraphQL",
    "openai": "OpenAI",
    "devops": "DevOps",
    "powerpoint": "PowerPoint",
    "linkedin": "LinkedIn",
    "excel": "Excel",
    "salesforce": "Salesforce",
    "kubernetes": "Kubernetes",
    "docker": "Docker",
    "python": "Python",
}


def display_skill(skill: str) -> str:
    """One skill, cased for a reader. Never invents; only repairs lowercase."""
    words = str(skill or "").strip().split()
    out: list[str] = []
    for index, word in enumerate(words):
        low = word.lower()
        if word != low:
            # The author already cased it ("iOS", "LaTeX"); their casing wins
            # over every rule below.
            out.append(word)
        elif low in _BRANDS:
            out.append(_BRANDS[low])
        elif low in _ACRONYMS:
            out.append(word.upper())
        elif index == 0:
            out.append(word[:1].upper() + word[1:])
        else:
            out.append(word)
    return " ".join(out)


def _display_date(value: object) -> str:
    """``2021-03`` → ``03/2021``, ``present`` → ``Present``, else as written."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower() in {"present", "current", "now"}:
        return "Present"
    matched = re.match(r"^(\d{4})-(\d{2})$", text)
    if matched:
        return f"{matched.group(2)}/{matched.group(1)}"
    return text


def _date_span(start: object, end: object) -> str:
    left, right = _display_date(start), _display_date(end)
    if left and right:
        return f"{left} – {right}"  # noqa: RUF001 - the en dash is the typographic convention
    return left or right


def _two_columns(left: Paragraph, right: Paragraph) -> Table:
    return Table(
        [[left, right]],
        colWidths=[_CONTENT_WIDTH * 0.64, _CONTENT_WIDTH * 0.36],
        style=TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        ),
    )


def _bullet_list(bullets: list) -> ListFlowable:
    return ListFlowable(
        [ListItem(Paragraph(_escape(b), _BULLET), leftIndent=12) for b in bullets],
        bulletType="bullet",
        start="•",
        leftIndent=12,
    )


def _entry_flowables(job: dict) -> list:
    """One experience-or-leadership entry: two ruled columns, then bullets."""
    flows: list = [Spacer(1, 5)]
    company = _escape(job.get("company"))
    title = _escape(job.get("title"))
    location = _escape(job.get("location"))
    span = _escape(_date_span(job.get("start"), job.get("end")))
    top_left = company or title
    if top_left:
        flows.append(_two_columns(Paragraph(top_left, _ROLE), Paragraph(location, _ROLE_R)))
    if company and (title or span):
        flows.append(_two_columns(Paragraph(title, _TITLE), Paragraph(span, _DATES)))
    elif span:
        flows.append(_two_columns(Paragraph("", _TITLE), Paragraph(span, _DATES)))
    bullets = [b for b in (job.get("bullets") or []) if str(b).strip()]
    if bullets:
        flows.append(_bullet_list(bullets))
    return flows


_DETAIL_LABEL = re.compile(r"^([A-Za-z][A-Za-z &/]{1,24}):\s*(.+)$")


def _labeled_line(text: str) -> Paragraph:
    """"Honors: Dean's List" with the label bold, as the reference resumes do."""
    matched = _DETAIL_LABEL.match(text.strip())
    if matched:
        return Paragraph(
            f"<b>{_escape(matched.group(1))}:</b> {_escape(matched.group(2))}", _BODY
        )
    return Paragraph(_escape(text), _BODY)


def _education_flowables(edu: dict) -> list:
    flows: list = [Spacer(1, 5)]
    school = _escape(edu.get("school"))
    degree_field = ", ".join(
        x for x in (_escape(edu.get("degree")), _escape(edu.get("field"))) if x
    )
    years = _escape(
        _date_span(str(edu.get("start_year") or ""), str(edu.get("end_year") or ""))
    )
    if school:
        flows.append(_two_columns(Paragraph(school, _ROLE), Paragraph("", _ROLE_R)))
    if degree_field or years:
        flows.append(_two_columns(Paragraph(degree_field, _TITLE), Paragraph(years, _DATES)))
    for line in edu.get("details") or []:
        if str(line).strip():
            flows.append(_labeled_line(str(line)))
    return flows


def _career_span_years(experience: list, today: date) -> float | None:
    """Earliest start to latest end (or today), in years. None when undatable."""

    def months_of(value: object, default_month: int) -> int | None:
        matched = re.match(r"^(\d{4})(?:-(\d{2}))?", str(value or "").strip())
        if not matched:
            return None
        return int(matched.group(1)) * 12 + int(matched.group(2) or default_month)

    starts: list[int] = []
    ends: list[int] = []
    for job in experience:
        if not isinstance(job, dict):
            continue
        start = months_of(job.get("start"), 1)
        if start is not None:
            starts.append(start)
        end_raw = str(job.get("end") or "").strip().lower()
        if end_raw in {"present", "current", "now"}:
            ends.append(today.year * 12 + today.month)
        else:
            end = months_of(job.get("end"), 12)
            if end is not None:
                ends.append(end)
    if not starts or not ends:
        return None
    return max(0, max(ends) - min(starts)) / 12.0


def _education_first(structured: dict, today: date) -> bool:
    """Does this record read as a student or brand-new graduate?

    Education leads when they are still in school (an end year at or past
    this one) or the whole professional history spans under ~18 months —
    the campus-resume convention. Everyone else leads with experience; a
    ten-year operator doing an executive program does not become a student.
    """
    education = structured.get("education") or []
    experience = structured.get("experience") or []
    if not education:
        return False
    if not experience:
        return True
    span = _career_span_years(experience, today)
    if span is not None and span >= 2.0:
        return False
    for edu in education:
        if not isinstance(edu, dict):
            continue
        matched = re.match(r"^(\d{4})", str(edu.get("end_year") or "").strip())
        if matched and int(matched.group(1)) >= today.year:
            return True
    return span is not None and span < 1.5


def render_resume_pdf(structured: dict, *, today: date | None = None) -> bytes:
    """One resume, letter-size, from a ``resume.v1`` dict."""
    today = today or date.today()
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
        story.append(Paragraph(_escape(contact["name"]).upper(), _NAME))
    contact_bits = [
        _escape(contact.get(key))
        for key in ("phone", "email", "location", "linkedin_url", "github_url", "website_url")
        if contact.get(key)
    ]
    if contact_bits:
        story.append(Paragraph(" | ".join(contact_bits), _CONTACT))
    story.append(Spacer(1, 4))

    # LinkedIn gives us a headline while an uploaded resume may give a terse
    # but meaningful summary. Preserve either when it is part of the supplied
    # resume shape: dropping a stated fact to make a page look leaner is not a
    # permissible formatting decision. Student profiles normally have this
    # field empty, which naturally produces the education-first page.
    summary = (structured.get("summary") or "").strip()
    if summary:
        story.append(Paragraph("SUMMARY", _HEADING))
        story.append(_rule())
        story.append(Paragraph(_escape(summary), _BODY))

    def experience_section() -> None:
        entries = [j for j in (structured.get("experience") or []) if isinstance(j, dict)]
        if not entries:
            return
        story.append(Paragraph("PROFESSIONAL EXPERIENCE", _HEADING))
        story.append(_rule())
        for job in entries:
            story.extend(_entry_flowables(job))

    def education_section() -> None:
        entries = [e for e in (structured.get("education") or []) if isinstance(e, dict)]
        if not entries:
            return
        story.append(Paragraph("EDUCATION", _HEADING))
        story.append(_rule())
        for edu in entries:
            story.extend(_education_flowables(edu))

    def leadership_section() -> None:
        entries = [j for j in (structured.get("leadership") or []) if isinstance(j, dict)]
        if not entries:
            return
        story.append(Paragraph("LEADERSHIP & INVOLVEMENT", _HEADING))
        story.append(_rule())
        for job in entries:
            story.extend(_entry_flowables(job))

    if _education_first(structured, today):
        education_section()
        experience_section()
    else:
        experience_section()
        education_section()
    leadership_section()

    def cleaned(key: str) -> list[str]:
        return [str(x).strip() for x in (structured.get(key) or []) if str(x).strip()]

    labeled: list[tuple[str, str]] = []
    certifications = cleaned("certifications")
    if certifications:
        labeled.append(("Licenses & Certifications", ", ".join(certifications)))
    skills = cleaned("skills")
    if skills:
        labeled.append(("Skills", ", ".join(display_skill(s) for s in skills)))
    languages = cleaned("languages")
    if languages:
        labeled.append(("Languages", ", ".join(display_skill(x) for x in languages)))
    interests = cleaned("interests")
    if interests:
        labeled.append(("Interests", ", ".join(display_skill(x) for x in interests)))
    if labeled:
        story.append(Paragraph("SKILLS & INTERESTS", _HEADING))
        story.append(_rule())
        story.append(Spacer(1, 2))
        for label, value in labeled:
            story.append(Paragraph(f"<b>{_escape(label)}:</b> {_escape(value)}", _BODY))

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
