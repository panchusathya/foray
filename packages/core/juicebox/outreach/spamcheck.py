"""Spam-risk score for an outreach email, before it is sent.

Deterministic and offline, like the rest of the stack: no API key, no call to a
third-party tester. It reads what a receiving filter reads -- the subject and
the HTML exactly as the send path would produce it, tracking included -- and
adds up the signals that push cold mail toward the junk folder.

It is a checklist with weights, not a model of Gmail. The number is only a
summary; what earns its place is each finding naming the words that tripped it
and what to write instead. Two families are kept apart on purpose:

* ``content`` -- the copy. The recruiter can fix these in the editor.
* ``delivery`` -- how the email is sent (the open-tracking pixel, rewritten
  links). Nothing in the editor changes them, so they are reported separately
  rather than folded into a score the copy can never improve.

Recruiting mail legitimately talks about pay, equity and deadlines, so money
and urgency are weighted as nudges, not verdicts: one mention costs little,
stacking them in a first email is what reads as a pitch.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from . import templates

LOW_BELOW = 15
HIGH_FROM = 35

# What a template variable looks like once it is filled, so the checker sees
# a sentence rather than braces. Values are deliberately bland.
_SAMPLE = {
    "first_name": "Alex",
    "last_name": "Morgan",
    "full_name": "Alex Morgan",
    "current_title": "Software Engineer",
    "current_company": "Acme",
    "top_skill": "Python",
    "role_title": "Senior Engineer",
    "project_name": "the role",
    "location": "San Francisco",
    "opening_line": "Your work on the payments platform at Acme stood out.",
    "why_relevant": "The team needs someone who has scaled a platform like yours.",
    "subject_hint": "A quick note",
    "sender_first_name": "Sam",
    "sender_name": "Sam Lee",
    "suggested_day": "Thursday",
    "suggested_date": "Thursday, October 2",
}
_AI_SAMPLE = "A sentence written for this person."
# Variables that only change the greeting or the signature. A body whose only
# variables are these is the same email to everybody.
_SURFACE_VARIABLES = frozenset(
    {"first_name", "last_name", "full_name", "sender_first_name", "sender_name"}
)

_ACRONYMS = frozenset(
    {
        "AI",
        "ML",
        "SF",
        "NYC",
        "LA",
        "US",
        "USA",
        "UK",
        "EU",
        "SAP",
        "AWS",
        "GCP",
        "API",
        "SQL",
        "CTO",
        "CEO",
        "CFO",
        "COO",
        "VP",
        "HR",
        "IPO",
        "SAAS",
        "B2B",
        "LLM",
        "LLMS",
        "GPU",
        "GPUS",
        "IC",
        "PM",
        "QA",
        "UX",
        "UI",
        "IOS",
        "OK",
        "PHD",
        "MBA",
        "YC",
        "ARR",
        "R&D",
        "NLP",
        "CV",
        "RL",
        "SRE",
        "DEVOPS",
        "ASAP",
    }
)

# (pattern, label). Matched case-insensitively on words, so "free" does not
# match "freedom". Recruiting-flavoured entries are the ones cold recruiting
# mail actually trips on; the rest are the classic filter lists.
_TRIGGERS: tuple[tuple[str, str], ...] = (
    (r"(?<!feel )\bfree\b(?! to\b)", "free"),
    (r"\b100 ?%", "100%"),
    (r"\bguarantee[ds]?\b", "guarantee"),
    (r"\brisk[- ]free\b", "risk-free"),
    (r"\bno obligation\b", "no obligation"),
    (r"\bact now\b", "act now"),
    (r"\bclick (?:here|below|the link)\b", "click here"),
    (r"\b(?:buy|order|apply|sign up|register) now\b", "apply/buy now"),
    (r"\blimited time\b", "limited time"),
    (r"\b(?:special|exclusive) (?:offer|deal|opportunity)\b", "exclusive offer"),
    (r"\bcongratulations\b", "congratulations"),
    (r"\byou(?:'ve| have) been (?:selected|chosen)\b", "you have been selected"),
    (r"\b(?:make|earn) (?:money|\$)", "make money"),
    (r"\bextra income\b", "extra income"),
    (r"\bwork from home\b", "work from home"),
    (r"\bonce in a lifetime\b", "once in a lifetime"),
    (r"\bopportunity of a lifetime\b", "opportunity of a lifetime"),
    (r"\bamazing opportunity\b", "amazing opportunity"),
    (r"\bdream job\b", "dream job"),
    (r"\bhiring immediately\b", "hiring immediately"),
    (r"\bdon'?t miss\b", "don't miss"),
    (r"\bthis (?:is not|isn'?t) spam\b", "this isn't spam"),
    (r"\bdear (?:friend|candidate|sir|madam)\b", "generic greeting"),
    (r"\bto whom it may concern\b", "to whom it may concern"),
    (r"\bcash\b", "cash"),
    (r"\bmillion dollars\b", "million dollars"),
    (r"\bwinner\b", "winner"),
)
_URGENCY: tuple[tuple[str, str], ...] = (
    (r"\burgent(?:ly)?\b", "urgent"),
    (r"\bimmediately\b", "immediately"),
    (r"\basap\b", "ASAP"),
    (r"\blast chance\b", "last chance"),
    (r"\bdeadline\b", "deadline"),
    (r"\bend of (?:the )?(?:day|week)\b", "end of day/week"),
    (r"\bby (?:today|tonight|tomorrow)\b", "by today/tomorrow"),
    (r"\b(?:expires?|expiring)\b", "expires"),
    (r"\bright away\b", "right away"),
)
_MONEY = re.compile(
    r"(?:[$€£]\s?\d[\d,.]*k?)|(?:\${2,})|(?:\b\d{2,3}(?:\.\d)?\s?k\b)|(?:\b\d+(?:,\d{3})+\b)|"
    r"(?:\b(?:salary|compensation|comp|paying|pays)\b)",
    re.IGNORECASE,
)
_SHORTENERS = re.compile(
    r"https?://(?:www\.)?(?:bit\.ly|tinyurl\.com|t\.co|goo\.gl|ow\.ly|is\.gd|buff\.ly|"
    r"rebrand\.ly|cutt\.ly|shorturl\.at|tiny\.cc|lnkd\.in)/",
    re.IGNORECASE,
)
_ANCHOR = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile("[A-Za-z][A-Za-z&'’-]*")  # noqa: RUF001 - curly apostrophes are words
_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")
_FAKE_REPLY = re.compile(r"^\s*(?:re|fwd?|fw)\s*:", re.IGNORECASE)


@dataclass
class SpamFinding:
    code: str
    area: str  # subject | body | delivery
    points: int
    title: str
    detail: str
    fix: str = ""
    matches: list[str] = field(default_factory=list)


@dataclass
class SpamReport:
    score: int
    level: str  # low | medium | high
    content_score: int
    delivery_score: int
    findings: list[SpamFinding]
    word_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def level_for(score: int) -> str:
    if score < LOW_BELOW:
        return "low"
    if score < HIGH_FROM:
        return "medium"
    return "high"


def fill_template(template: str) -> str:
    """Replace variables and AI directives with plausible sample text."""
    text = templates._AI_RE.sub(_AI_SAMPLE, template or "")

    def variable(match: re.Match[str]) -> str:
        name, fallback = match.group(1), match.group(2)
        return _SAMPLE.get(name) or (fallback or "").strip() or "sample"

    return templates._VAR_RE.sub(variable, text)


def personalization_depth(template: str) -> str:
    """none | surface (name only) | deep (anything that changes the sentence)."""
    if templates._AI_RE.search(template or ""):
        return "deep"
    names = set(templates.used_variables(template or ""))
    if not names:
        return "none"
    return "surface" if names <= _SURFACE_VARIABLES else "deep"


def _hits(patterns: tuple[tuple[str, str], ...], text: str) -> list[str]:
    found = []
    for pattern, label in patterns:
        if re.search(pattern, text, re.IGNORECASE) and label not in found:
            found.append(label)
    return found


def _shouty(text: str) -> list[str]:
    return [
        word
        for word in _WORD.findall(text)
        if len(word) >= 3 and word.isupper() and word.upper() not in _ACRONYMS
    ]


def _domain(url: str) -> str:
    match = re.match(r"https?://([^/:?#]+)", url.strip(), re.IGNORECASE)
    return (match.group(1).lower().removeprefix("www.")) if match else ""


def score_email(
    *,
    subject: str,
    body: str,
    is_follow_up: bool = False,
    personalization: str | None = None,
    open_tracking: bool = True,
    click_tracking: bool = True,
) -> SpamReport:
    """Score one rendered email. ``body`` may be editor HTML or plain text."""
    html = templates.to_html(body or "")
    text = templates.html_to_text(html)
    words = _WORD.findall(text)
    findings: list[SpamFinding] = []

    def add(finding: SpamFinding) -> None:
        if finding.points > 0:
            findings.append(finding)

    # -- subject ---------------------------------------------------------
    subject = (subject or "").strip()
    if not is_follow_up:
        if not subject:
            add(
                SpamFinding(
                    "subject_empty",
                    "subject",
                    15,
                    "No subject line",
                    "Mail with a blank subject is filtered far more often.",
                    "Write a short, specific subject.",
                )
            )
        else:
            if _FAKE_REPLY.match(subject):
                add(
                    SpamFinding(
                        "subject_fake_reply",
                        "subject",
                        15,
                        "First email starts with Re: or Fwd:",
                        "Pretending to be a reply is a classic spam trick, and filters look for it.",
                        "Drop the Re:/Fwd: prefix on the first email.",
                    )
                )
            if len(subject) > 70:
                add(
                    SpamFinding(
                        "subject_long",
                        "subject",
                        5,
                        "Long subject",
                        f"{len(subject)} characters; phones cut it at about 40.",
                        "Keep it under ~60 characters.",
                    )
                )
            bangs = subject.count("!")
            if bangs:
                add(
                    SpamFinding(
                        "subject_exclaim",
                        "subject",
                        8 if bangs == 1 else 15,
                        "Exclamation mark in the subject",
                        "Punctuation that shouts is one of the oldest spam rules.",
                        "Remove the exclamation mark.",
                    )
                )
            caps = _shouty(subject)
            if caps:
                add(
                    SpamFinding(
                        "subject_caps",
                        "subject",
                        10,
                        "Capitals in the subject",
                        "Words in all capitals read as shouting to filters.",
                        "Use normal capitalisation.",
                        caps[:5],
                    )
                )
            if _MONEY.search(subject):
                add(
                    SpamFinding(
                        "subject_money",
                        "subject",
                        8,
                        "Money in the subject",
                        "Pay figures or $ in a subject line are a strong promotional signal.",
                        "Keep compensation in the body.",
                    )
                )
            triggers = _hits(_TRIGGERS, subject)
            if triggers:
                add(
                    SpamFinding(
                        "subject_triggers",
                        "subject",
                        min(10 * len(triggers), 25),
                        "Spam trigger words in the subject",
                        "These phrases are on the lists filters score against.",
                        "Reword them.",
                        triggers,
                    )
                )
            if _EMOJI.search(subject):
                add(
                    SpamFinding(
                        "subject_emoji",
                        "subject",
                        3,
                        "Emoji in the subject",
                        "Emoji in a cold subject look promotional.",
                        "Leave them out of first-touch mail.",
                    )
                )

    # -- body ------------------------------------------------------------
    triggers = _hits(_TRIGGERS, text)
    if triggers:
        add(
            SpamFinding(
                "body_triggers",
                "body",
                min(5 * len(triggers), 20),
                "Spam trigger words",
                "These phrases are on the lists filters score against.",
                "Say the same thing in plainer words.",
                triggers,
            )
        )
    urgency = _hits(_URGENCY, text)
    if urgency:
        add(
            SpamFinding(
                "body_urgency",
                "body",
                min(3 * len(urgency), 9),
                "Urgency language",
                "Deadlines and pressure words are a common spam pattern; one is fine, several add up.",
                'Keep at most one, and soften it ("this week" rather than "end of day").',
                urgency,
            )
        )
    money = sorted({m.group(0).strip() for m in _MONEY.finditer(text)})
    if len(money) >= 3:
        add(
            SpamFinding(
                "body_money",
                "body",
                5,
                "Lots of pay figures",
                "Several money amounts in one email reads as a sales pitch.",
                "State pay once, as a range.",
                money[:6],
            )
        )
    elif money:
        add(
            SpamFinding(
                "body_money",
                "body",
                2,
                "Pay figure mentioned",
                "Fine for recruiting mail; noted because it adds up with other signals.",
                "",
                money[:6],
            )
        )
    bangs = text.count("!")
    if bangs > 1:
        add(
            SpamFinding(
                "body_exclaim",
                "body",
                3 if bangs <= 3 else 8,
                "Exclamation marks",
                f"{bangs} in the body.",
                "Keep at most one.",
            )
        )
    caps = _shouty(text)
    if len(caps) >= 3 or (words and len(caps) / len(words) > 0.1):
        add(
            SpamFinding(
                "body_caps",
                "body",
                8,
                "Words in all capitals",
                "Capitalised words read as shouting.",
                "Use normal capitalisation.",
                caps[:6],
            )
        )

    links = _ANCHOR.findall(html)
    if len(links) >= 3:
        add(
            SpamFinding(
                "body_links",
                "body",
                10,
                "Too many links",
                f"{len(links)} links. Cold emails with several links are filtered hard.",
                "Use one link at most, or none: ask for a reply instead.",
            )
        )
    elif len(links) == 2:
        add(
            SpamFinding(
                "body_links",
                "body",
                4,
                "Two links",
                "Every link in a first email adds risk.",
                "Keep one, or none.",
            )
        )
    shortened = [href for href, _ in links if _SHORTENERS.match(href)]
    if shortened:
        add(
            SpamFinding(
                "body_shortener",
                "body",
                15,
                "Shortened link",
                "bit.ly-style links hide the destination, and filters treat them as suspect.",
                "Link to the real address.",
                shortened[:3],
            )
        )
    mismatched = []
    for href, label in links:
        label_text = _TAG.sub("", label).strip()
        if re.match(r"(?:https?://|www\.)", label_text, re.IGNORECASE):
            shown, real = (
                _domain(label_text if "://" in label_text else f"http://{label_text}"),
                _domain(href),
            )
            if shown and real and shown != real:
                mismatched.append(label_text)
    if mismatched:
        add(
            SpamFinding(
                "body_link_mismatch",
                "body",
                12,
                "Link text shows a different address",
                "Text that shows one address while linking to another is a phishing signal.",
                "Make the visible address match the link, or use plain words as the link text.",
                mismatched[:3],
            )
        )

    count = len(words)
    if count and count < 20:
        add(
            SpamFinding(
                "body_short",
                "body",
                4,
                "Very short email",
                f"{count} words. A few words plus a tracking image looks automated.",
                "Add a sentence or two of real context.",
            )
        )
    elif count > (150 if is_follow_up else 220):
        add(
            SpamFinding(
                "body_long",
                "body",
                5,
                "Long email",
                f"{count} words. Long cold emails get fewer replies, and replies are what build a sender's reputation.",
                "Cut it to the few lines that matter.",
            )
        )

    if personalization == "none":
        add(
            SpamFinding(
                "body_identical",
                "body",
                8,
                "Same text to everybody",
                "No variables at all. Identical bodies sent at volume are easy for filters to fingerprint as bulk.",
                "Add {{ first_name }} and ideally {{ opening_line }} or an {{ai: …}} line.",
            )
        )
    elif personalization == "surface":
        add(
            SpamFinding(
                "body_surface",
                "body",
                4,
                "Only the name changes",
                "Apart from the name, every recipient gets the same words, which filters can fingerprint as bulk.",
                "Add {{ opening_line }}, {{ why_relevant }} or an {{ai: …}} line so each email differs.",
            )
        )

    # -- delivery ----------------------------------------------------------
    if open_tracking:
        add(
            SpamFinding(
                "delivery_pixel",
                "delivery",
                10,
                "Hidden tracking image",
                "Every email carries a hidden 1x1 image for open tracking. Gmail and Outlook "
                "treat hidden remote images as a bulk-mail signal, most of all in short, "
                "plain emails like cold outreach.",
                "Not editable here: it is added to every send.",
            )
        )
    if click_tracking and links:
        add(
            SpamFinding(
                "delivery_click_tracking",
                "delivery",
                min(4 * len(links), 12),
                "Links go through a tracking redirect",
                "Each link is rewritten to pass through our click tracker, so the address a "
                "filter sees is not the one you wrote.",
                "Fewer links means fewer redirects; none is safest.",
            )
        )

    content = sum(f.points for f in findings if f.area != "delivery")
    delivery = sum(f.points for f in findings if f.area == "delivery")
    total = min(content + delivery, 100)
    findings.sort(key=lambda f: (-f.points, f.code))
    return SpamReport(
        score=total,
        level=level_for(total),
        content_score=min(content, 100),
        delivery_score=min(delivery, 100),
        findings=findings,
        word_count=count,
    )


def score_template(
    subject_template: str, body_template: str, *, is_follow_up: bool = False
) -> SpamReport:
    """Score a step as written, with sample values in place of variables."""
    return score_email(
        subject=fill_template(subject_template),
        body=fill_template(body_template),
        is_follow_up=is_follow_up,
        personalization=personalization_depth(f"{subject_template}\n{body_template}"),
    )
