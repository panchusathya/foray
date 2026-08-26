"""``resume.v1`` JSON → PDF bytes, and a letter to match.

reportlab platypus: real pagination for a two-page history, no HTML engine,
no system packages. One clean template on purpose — the differentiator is the
content pipeline, not a theme gallery, and every escaping bug lives in the
one place ``_escape`` is.

The layout follows the classic finance/consulting resume: centered name and
contact line, left-aligned ruled sections, two-column entry rows (organization
bold left, location bold right; title italic left, dates italic right), and
a single SKILLS & INTERESTS block of labeled lines. Section ORDER flexes
with the person: professional experience leads, unless the record reads as a
student or brand-new graduate — still in school, or under ~18 months of
history — in which case education leads, the convention every campus career
center teaches. A leadership section appears exactly when the data has one
and takes whatever room its entries need.

Section order is not the only thing that flexes. **The page setting does too.**
A resume is read as a one-page document, so one fixed type size and one fixed
set of gaps is the wrong choice at both ends: a three-role career printed at
the density a fifteen-role career needs leaves four inches of white below the
last line and reads as an unfinished draft, and a long history printed at the
density a short one wants spills two orphaned lines onto a second page nobody
wanted. ``_LADDER`` runs tight to airy — margins, type size, leading and gaps
moving together the way a typesetter moves them — and ``render_resume_pdf``
draws at the airiest setting that still lands inside the page budget. Filling
the page is a typographic decision taken over the same facts; nothing is added
or dropped to change the length.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date
from xml.sax.saxutils import escape as _xml_escape
from xml.sax.saxutils import quoteattr

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

_MARGIN = 0.50 * inch
# SimpleDocTemplate's frame adds six points of padding on each side. Story
# paragraphs already live inside that frame; tables do not shrink themselves
# to it, so using the raw margin width made every two-column row hang 6pt past
# the section rules on both sides.
_FRAME_PADDING = 12


@dataclass(frozen=True)
class _Layout:
    """One setting of the page, described the way a typesetter would.

    ``margin`` is in points; the other three are multipliers on the reference
    resume's type sizes, leading and gaps. Wider margins fill a page as much
    as larger type does — they shorten the column, so the same words wrap to
    more lines *and* have less height to cover.
    """

    margin: float
    type: float
    lead: float
    gap: float

    @property
    def content_width(self) -> float:
        return LETTER[0] - 2 * self.margin - _FRAME_PADDING


# Tight to airy. The tight end is a real finance resume — 9pt with the gaps
# closed up, which is how a fifteen-year history gets set by hand; the airy
# end is 11pt on generous margins, which is how a two-internship resume gets
# set by hand. Neither end is a page nobody would print.
_LADDER: tuple[_Layout, ...] = (
    _Layout(margin=0.50 * inch, type=0.90, lead=0.98, gap=0.55),
    _Layout(margin=0.50 * inch, type=0.94, lead=1.00, gap=0.72),
    _Layout(margin=0.50 * inch, type=0.97, lead=1.00, gap=0.86),
    _Layout(margin=0.50 * inch, type=1.00, lead=1.00, gap=1.00),
    _Layout(margin=0.60 * inch, type=1.00, lead=1.04, gap=1.20),
    _Layout(margin=0.70 * inch, type=1.02, lead=1.08, gap=1.45),
    _Layout(margin=0.80 * inch, type=1.05, lead=1.12, gap=1.75),
    _Layout(margin=0.90 * inch, type=1.08, lead=1.16, gap=2.10),
    _Layout(margin=1.00 * inch, type=1.10, lead=1.20, gap=2.50),
)
# The setting the rest of this module is described in terms of: multipliers of
# 1.0 on the original margin, so `_REFERENCE_STYLES` is the resume exactly as
# it was drawn before the ladder existed.
_REFERENCE = _LADDER[3]
_CONTENT_WIDTH = _REFERENCE.content_width


class _Styles:
    """Every paragraph style for one ``_Layout``, scaled off the reference."""

    def __init__(self, layout: _Layout) -> None:
        self.layout = layout

        def size(points: float) -> float:
            return points * layout.type

        def leading(points: float) -> float:
            # Leading scales with the type first, then again with `lead`, so a
            # setting can open the lines up without also enlarging the letters.
            return points * layout.type * layout.lead

        self.name = ParagraphStyle(
            "name",
            fontName="Times-Bold",
            fontSize=size(16),
            leading=leading(19),
            alignment=TA_CENTER,
        )
        self.contact = ParagraphStyle(
            "contact",
            fontName="Times-Roman",
            fontSize=size(9),
            leading=leading(11),
            textColor="#000000",
            alignment=TA_CENTER,
        )
        self.heading = ParagraphStyle(
            "heading",
            fontName="Times-Bold",
            fontSize=size(10),
            leading=leading(13),
            spaceBefore=9 * layout.gap,
            spaceAfter=2 * layout.gap,
        )
        self.role = ParagraphStyle(
            "role",
            fontName="Times-Bold",
            fontSize=size(10),
            leading=leading(12),
            alignment=TA_LEFT,
        )
        self.role_right = ParagraphStyle("role_r", parent=self.role, alignment=TA_RIGHT)
        self.title = ParagraphStyle(
            "title",
            fontName="Times-Italic",
            fontSize=size(10),
            leading=leading(12),
            alignment=TA_LEFT,
        )
        self.dates = ParagraphStyle(
            "dates",
            fontName="Times-Italic",
            fontSize=size(9),
            leading=leading(12),
            textColor="#000000",
            alignment=TA_RIGHT,
        )
        self.body = ParagraphStyle(
            "body", fontName="Times-Roman", fontSize=size(10), leading=leading(12)
        )
        self.bullet = ParagraphStyle("bullet", parent=self.body, leftIndent=0)

        self.entry_gap = 5 * layout.gap
        self.header_gap = 4 * layout.gap
        self.rule_gap = 4 * layout.gap
        self.skills_gap = 2 * layout.gap
        self.bullet_indent = 12 * layout.type


_REFERENCE_STYLES = _Styles(_REFERENCE)

# The reference styles under the names the module has always used: the resume
# as drawn at density 1.0, and the default every helper below falls back to.
_NAME = _REFERENCE_STYLES.name
_CONTACT = _REFERENCE_STYLES.contact
_HEADING = _REFERENCE_STYLES.heading
_ROLE = _REFERENCE_STYLES.role
_ROLE_R = _REFERENCE_STYLES.role_right
_TITLE = _REFERENCE_STYLES.title
_DATES = _REFERENCE_STYLES.dates
_BODY = _REFERENCE_STYLES.body
_BULLET = _REFERENCE_STYLES.bullet


def _escape(value: object) -> str:
    return _xml_escape(str(value or "").strip())


def _pdf_link(label: object, href: str) -> str:
    """Clickable, visibly linked text for ReportLab Paragraph markup."""
    return f'<link href={quoteattr(href)} color="#0000EE"><u>{_escape(label)}</u></link>'


def _web_href(value: object) -> str:
    text = str(value or "").strip()
    return text if re.match(r"^https?://", text, flags=re.IGNORECASE) else f"https://{text}"


def _rule(styles: _Styles = _REFERENCE_STYLES) -> HRFlowable:
    return HRFlowable(width="100%", thickness=0.6, color="#999999", spaceAfter=styles.rule_gap)


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


def _two_columns(
    left: Paragraph, right: Paragraph, width: float = _CONTENT_WIDTH
) -> Table:
    table = Table(
        [[left, right]],
        colWidths=[width * 0.64, width * 0.36],
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
    table.hAlign = "LEFT"
    return table


def _bullet_list(bullets: list, styles: _Styles = _REFERENCE_STYLES) -> ListFlowable:
    indent = styles.bullet_indent
    return ListFlowable(
        [ListItem(Paragraph(_escape(b), styles.bullet), leftIndent=indent) for b in bullets],
        bulletType="bullet",
        start="•",
        leftIndent=indent,
    )


def _entry_flowables(job: dict, styles: _Styles = _REFERENCE_STYLES) -> list:
    """One experience-or-leadership entry: two ruled columns, then bullets."""
    width = styles.layout.content_width
    flows: list = [Spacer(1, styles.entry_gap)]
    company = _escape(job.get("company"))
    title = _escape(job.get("title"))
    location = _escape(job.get("location"))
    span = _escape(_date_span(job.get("start"), job.get("end")))
    top_left = company or title
    if top_left:
        flows.append(
            _two_columns(
                Paragraph(top_left, styles.role),
                Paragraph(location, styles.role_right),
                width,
            )
        )
    if company and (title or span):
        flows.append(
            _two_columns(Paragraph(title, styles.title), Paragraph(span, styles.dates), width)
        )
    elif span:
        flows.append(
            _two_columns(Paragraph("", styles.title), Paragraph(span, styles.dates), width)
        )
    bullets = [b for b in (job.get("bullets") or []) if str(b).strip()]
    if bullets:
        flows.append(_bullet_list(bullets, styles))
    return flows


_DETAIL_LABEL = re.compile(r"^([A-Za-z][A-Za-z &/]{1,24}):\s*(.+)$")


def _labeled_line(text: str, styles: _Styles = _REFERENCE_STYLES) -> Paragraph:
    """"Honors: Dean's List" with the label bold, as the reference resumes do."""
    matched = _DETAIL_LABEL.match(text.strip())
    if matched:
        return Paragraph(
            f"<b>{_escape(matched.group(1))}:</b> {_escape(matched.group(2))}", styles.body
        )
    return Paragraph(_escape(text), styles.body)


def _education_flowables(edu: dict, styles: _Styles = _REFERENCE_STYLES) -> list:
    width = styles.layout.content_width
    flows: list = [Spacer(1, styles.entry_gap)]
    school = _escape(edu.get("school"))
    degree_field = ", ".join(
        x for x in (_escape(edu.get("degree")), _escape(edu.get("field"))) if x
    )
    years = _escape(
        _date_span(str(edu.get("start_year") or ""), str(edu.get("end_year") or ""))
    )
    if school:
        flows.append(
            _two_columns(Paragraph(school, styles.role), Paragraph("", styles.role_right), width)
        )
    if degree_field or years:
        flows.append(
            _two_columns(
                Paragraph(degree_field, styles.title), Paragraph(years, styles.dates), width
            )
        )
    for line in edu.get("details") or []:
        if str(line).strip():
            flows.append(_labeled_line(str(line), styles))
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


def _story(structured: dict, today: date, styles: _Styles) -> list:
    """The whole resume as fresh flowables, set at one density.

    Fresh on every call on purpose: platypus flowables carry wrap state, so a
    story cannot be measured at one density and then drawn at another.
    """
    contact = structured.get("contact") or {}
    story: list = []

    if contact.get("name"):
        story.append(Paragraph(_escape(contact["name"]).upper(), styles.name))
    contact_bits: list[str] = []
    if contact.get("phone"):
        contact_bits.append(_escape(contact["phone"]))
    if contact.get("email"):
        email = str(contact["email"]).strip()
        contact_bits.append(_pdf_link(email, f"mailto:{email}"))
    if contact.get("location"):
        contact_bits.append(_escape(contact["location"]))
    if contact.get("linkedin_url"):
        linkedin = str(contact["linkedin_url"]).strip()
        contact_bits.append(_pdf_link(linkedin, _web_href(linkedin)))
    for key in ("github_url", "website_url"):
        if contact.get(key):
            contact_bits.append(_escape(contact[key]))
    if contact_bits:
        story.append(Paragraph(" | ".join(contact_bits), styles.contact))
    story.append(Spacer(1, styles.header_gap))

    # LinkedIn gives us a headline while an uploaded resume may give a terse
    # but meaningful summary. Preserve either when it is part of the supplied
    # resume shape: dropping a stated fact to make a page look leaner is not a
    # permissible formatting decision. Student profiles normally have this
    # field empty, which naturally produces the education-first page.
    summary = (structured.get("summary") or "").strip()
    if summary:
        story.append(Paragraph("SUMMARY", styles.heading))
        story.append(_rule(styles))
        story.append(Paragraph(_escape(summary), styles.body))

    def experience_section() -> None:
        entries = [j for j in (structured.get("experience") or []) if isinstance(j, dict)]
        if not entries:
            return
        story.append(Paragraph("PROFESSIONAL EXPERIENCE", styles.heading))
        story.append(_rule(styles))
        for job in entries:
            story.extend(_entry_flowables(job, styles))

    def education_section() -> None:
        entries = [e for e in (structured.get("education") or []) if isinstance(e, dict)]
        if not entries:
            return
        story.append(Paragraph("EDUCATION", styles.heading))
        story.append(_rule(styles))
        for edu in entries:
            story.extend(_education_flowables(edu, styles))

    def leadership_section() -> None:
        entries = [j for j in (structured.get("leadership") or []) if isinstance(j, dict)]
        if not entries:
            return
        story.append(Paragraph("LEADERSHIP & INVOLVEMENT", styles.heading))
        story.append(_rule(styles))
        for job in entries:
            story.extend(_entry_flowables(job, styles))

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
        story.append(Paragraph("SKILLS & INTERESTS", styles.heading))
        story.append(_rule(styles))
        story.append(Spacer(1, styles.skills_gap))
        for label, value in labeled:
            story.append(Paragraph(f"<b>{_escape(label)}:</b> {_escape(value)}", styles.body))

    return story


class _CountingDoc(SimpleDocTemplate):
    """A document that remembers how many pages the story actually took."""

    pages_used = 0

    def afterPage(self) -> None:
        self.pages_used = max(self.pages_used, self.page)


def _draw(structured: dict, today: date, layout: _Layout, title: str) -> tuple[bytes, int]:
    """``(pdf bytes, page count)`` for one setting of the page."""
    buffer = io.BytesIO()
    doc = _CountingDoc(
        buffer,
        pagesize=LETTER,
        leftMargin=layout.margin,
        rightMargin=layout.margin,
        topMargin=layout.margin,
        bottomMargin=layout.margin,
        title=title,
    )
    doc.build(_story(structured, today, _Styles(layout)))
    return buffer.getvalue(), max(1, doc.pages_used)


def render_resume_pdf(structured: dict, *, today: date | None = None) -> bytes:
    """One resume, letter-size, from a ``resume.v1`` dict.

    Drawn at the airiest setting on ``_LADDER`` that still fits a single page,
    which is what "make it one page" means when the career is short. A history
    too long for that is allowed a second page and drawn at the airiest setting
    that fits *two*, so the overflow is a balanced spread rather than three
    orphaned lines. Only a career that will not fit two pages even squeezed is
    drawn tightest and left to run: cutting somebody's jobs to hit a page count
    is an editorial decision, and this function only sets type.
    """
    today = today or date.today()
    contact = structured.get("contact") or {}
    title = f"{contact.get('name') or 'Resume'} - Resume"

    drawn: dict[int, tuple[bytes, int]] = {}

    def at(index: int) -> tuple[bytes, int]:
        if index not in drawn:
            drawn[index] = _draw(structured, today, _LADDER[index], title)
        return drawn[index]

    def airiest_within(budget: int) -> int | None:
        """The highest ladder index still inside ``budget`` pages, or None.

        Airier settings never produce *fewer* pages, so "fits the budget" is
        true for a prefix of the ladder and false after it — which is a binary
        search, not a walk. That matters: walking from the airy end drafts the
        five-page settings of a long career before rejecting them, and those
        are the slowest drafts to make.
        """
        low, high, found = 0, len(_LADDER) - 1, None
        while low <= high:
            middle = (low + high) // 2
            if at(middle)[1] <= budget:
                found, low = middle, middle + 1
            else:
                high = middle - 1
        return found

    index = airiest_within(1)
    if index is None:
        # Too much career for one page. Take the airiest setting that fits two,
        # so the overflow is a balanced spread rather than three orphaned lines.
        index = airiest_within(2)
    if index is None:
        # It will not fit two pages even squeezed. Draw it tightest and let it
        # run — cutting jobs to hit a page count is not a typographic decision.
        index = 0
    return at(index)[0]


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
