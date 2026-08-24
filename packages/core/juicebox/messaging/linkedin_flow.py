"""LinkedIn-first short intake: phases, confirm copy, and the reduced gate.

Conversation cadence still lives in ``conversation.py``. This module is the
vocabulary that path reads: which flow a thread is on, what the scraper wrote,
and the canned batched messages. None of this is a prompt instruction — the
turn handler branches on ``thread.flow`` and ``last_field_asked``.
"""

from __future__ import annotations

from typing import Any

from ..enrich.linkedin_scrape import LinkedInProfile, is_ic_title, seniority_from_years
from ..enrich.taxonomy import normalize_seniority
from ..intake.schema import LINKEDIN_SHORT_KEYS, coverage, empty_record
from ..models import IntakeThread
from .records import merge_record

FLOW_FULL = "full"
FLOW_LINKEDIN_SHORT = "linkedin_short"

PHASE_INTRO = "intro"
PHASE_SCRAPING = "scraping"
PHASE_CONFIRM = "confirm"
PHASE_GAP_FILL = "gap_fill"
PHASE_DONE = "done"

# Pseudo-fields stored on asked_field. Namespaced so they cannot collide with
# intake.schema keys, same rule as contact.name.
BATCH_CONFIRM = "batch.confirm"
BATCH_GAP_FILL = "batch.gap_fill"

FLOW_META_KEY = "_linkedin"

MAX_GAP_FILL = 3

# Highest-value leftover questions after a scrape. Comp, timeline, and work
# auth are what a client actually blocks on; consent is last because a model
# must never invent it and a human can confirm later.
GAP_FILL_ORDER: tuple[str, ...] = (
    "compensation",
    "timeline",
    "work_authorization",
    "ok_to_share_with_clients",
    "spec.remote_ok",
    "spec.seniority",
    "spec.locations",
    "spec.job_titles",
)

GAP_FILL_LINES: dict[str, str] = {
    "compensation": "what are you targeting on comp (base is fine)?",
    "timeline": "how soon would you want to move?",
    "work_authorization": (
        "do you have work auth where you're looking, or would you need sponsorship?"
    ),
    "ok_to_share_with_clients": (
        "ok for us to put your profile in front of companies we're hiring for?"
    ),
    "spec.remote_ok": "fully remote, hybrid, or in-office?",
    "spec.seniority": "what level are you aiming for?",
    "spec.locations": "where are you based, and which locations would you work in?",
    "spec.job_titles": "what kind of role are you looking for next?",
}

YEARS_LINE = "roughly how many years of experience do you have?"
# No leading "and": this is rendered as the first bullet of a dashed list,
# where it came out as "- and what name should i put on this?".
NAME_LINE = "what name should i put on this?"


def default_flow(channel: str) -> str:
    from .channels import LINQ

    return FLOW_LINKEDIN_SHORT if channel == LINQ else FLOW_FULL


def is_linkedin_short(thread: IntakeThread) -> bool:
    return (thread.flow or FLOW_FULL) == FLOW_LINKEDIN_SHORT


def flow_meta(thread: IntakeThread) -> dict[str, Any]:
    return dict((thread.record or {}).get(FLOW_META_KEY) or {})


def update_flow_meta(thread: IntakeThread, **kwargs: Any) -> None:
    """Reassign ``record`` so SQLAlchemy notices the JSON mutation."""
    record = dict(thread.record or {})
    meta = dict(record.get(FLOW_META_KEY) or {})
    meta.update(kwargs)
    record[FLOW_META_KEY] = meta
    thread.record = record


def scraped_profile(thread: IntakeThread) -> dict[str, Any]:
    payload = flow_meta(thread).get("scrape") or {}
    return payload if isinstance(payload, dict) else {}


def apply_profile_to_record(record: dict[str, Any] | None, profile: LinkedInProfile) -> dict:
    """Fill intake fields the scrape is allowed to guess.

    Seniority is filled only for management titles. ICs are asked years of
    experience instead — a title of "software engineer" does not say whether
    they are junior or staff, and inventing a level is the expensive miss.
    """
    incoming = empty_record()
    if profile.current_title:
        incoming["spec"]["job_titles"] = [profile.current_title]
    if profile.location:
        incoming["spec"]["locations"] = [profile.location]
    if profile.current_title and not is_ic_title(profile.current_title):
        level = normalize_seniority(profile.current_title)
        if level in {"director", "vp", "cxo"}:
            incoming["spec"]["seniority"] = [level]
    return merge_record(record, incoming)


def apply_years_to_record(record: dict[str, Any] | None, years: float) -> dict:
    incoming = empty_record()
    incoming["spec"]["seniority"] = [seniority_from_years(years)]
    return merge_record(record, incoming)


def short_coverage(record: dict[str, Any] | None):
    return coverage(record or {}, gate_keys=LINKEDIN_SHORT_KEYS)


def short_missing(thread: IntakeThread) -> list[str]:
    state = short_coverage(thread.record)
    return [key for key in GAP_FILL_ORDER if key in state.required_missing]


def wants_years_question(thread: IntakeThread) -> bool:
    """IC path: ask years instead of 'what level / management'."""
    if "spec.seniority" not in short_missing(thread):
        return False
    scrape = scraped_profile(thread)
    title = str(scrape.get("current_title") or "")
    if title:
        return is_ic_title(title)
    # No title at all — still prefer years over a seniority taxonomy question.
    return True


def confirm_message(profile: LinkedInProfile | dict[str, Any]) -> str:
    data = profile.to_dict() if isinstance(profile, LinkedInProfile) else dict(profile or {})
    bits: list[str] = []
    title = str(data.get("current_title") or "").strip()
    employer = str(data.get("current_employer") or "").strip()
    location = str(data.get("location") or "").strip()
    years = data.get("experience_years")
    if title and employer:
        bits.append(f"you're a {title} at {employer}")
    elif title:
        bits.append(f"you're a {title}")
    elif employer:
        bits.append(f"you're at {employer}")
    if location:
        bits.append(f"based in {location}")
    if isinstance(years, (int, float)) and years:
        bits.append(f"about {int(years)} years in")
    if not bits:
        return (
            "got your linkedin — i'll match you off that. anything i should know "
            "that's not on the profile?"
        )
    joined = ", ".join(bits)
    return (
        f"got it — {joined}. that's what we'll match you on, right? "
        f"if anything's off just say so."
    )


def scrape_failed_message() -> str:
    return (
        "couldn't pull your linkedin just now — no worries. "
        "mind answering a couple of quick questions so we can still match you?"
    )


def intro_message(*, recruiter: str, org: str) -> str:
    """The first thing a stranger ever reads from us.

    CANDIDATE_AGENT_PLAN §2 requires this message to carry three things,
    because one number serves every tenant and the candidate cannot tell who
    is texting from the number alone: who we are, that this is an AI, and how
    to stop. The opt-out line is not decoration — a shared sender's
    complaint rate is what carriers score, and "how do I make this stop" is
    the question that turns into a complaint when it has no answer.
    """
    name = (recruiter or "vivian").strip() or "vivian"
    org_name = (org or "our team").strip() or "our team"
    return (
        f"hey, i'm {name} from {org_name}. i'm an ai helping the team match you "
        f"to roles — we'll ask a short series of questions so we don't waste "
        f"your time on ones that aren't a fit. reply STOP any time to opt out, "
        f"HELP for info.\n\n"
        f"first, what's the best email to reach you on?"
    )


def gap_fill_message(thread: IntakeThread, *, preface: str = "") -> tuple[str, list[str]]:
    """One batched message, at most ``MAX_GAP_FILL`` dashed questions.

    Returns ``(body, keys)``. ``keys`` is what the turn records as asked so
    wrap-up does not send this again.
    """
    missing = short_missing(thread)
    lines: list[str] = []
    keys: list[str] = []

    if not (thread.contact_name or "").strip():
        lines.append(f"- {NAME_LINE}")

    if (
        len(lines) < MAX_GAP_FILL
        and wants_years_question(thread)
        and "spec.seniority" in missing
    ):
        lines.append(f"- {YEARS_LINE}")
        keys.append("spec.seniority")

    for key in missing:
        if len(lines) >= MAX_GAP_FILL:
            break
        if key in keys:
            continue
        lines.append(f"- {GAP_FILL_LINES[key]}")
        keys.append(key)

    if not lines:
        return "", []

    head = preface.strip() or "a couple last things so we can actually match you:"
    body = head + "\n\n" + "\n".join(lines)
    return body, keys
