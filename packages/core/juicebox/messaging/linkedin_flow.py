"""LinkedIn-first short intake: phases, confirm copy, and the reduced gate.

Conversation cadence still lives in ``conversation.py``. This module is the
vocabulary that path reads: which flow a thread is on, what the scraper wrote,
and the canned batched messages. None of this is a prompt instruction — the
turn handler branches on ``thread.flow`` and ``last_field_asked``.
"""

from __future__ import annotations

import re
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
# The reply to "what should i be looking for instead?". Its own field so the
# answer can be bound to spec.job_titles directly rather than hoping the
# extractor recognises it.
BATCH_RETARGET = "batch.retarget"

FLOW_META_KEY = "_linkedin"

MAX_GAP_FILL = 3

# The only questions onboarding still asks, and only when the profile failed
# to answer them. Comp, timeline and work auth used to head this list; they
# are application questions and `applications.followups` asks them at the
# moment an application needs them, with the role in hand to say why.
GAP_FILL_ORDER: tuple[str, ...] = (
    "spec.job_titles",
    "spec.locations",
    "spec.seniority",
)

GAP_FILL_LINES: dict[str, str] = {
    "spec.job_titles": "what kind of role are you looking for next?",
    "spec.locations": "where are you based, and which locations would you work in?",
    "spec.seniority": "what level are you aiming for?",
}

YEARS_LINE = "roughly how many years of experience do you have?"
# Kept for the one place a name is still worth asking for: the manager, when
# somebody has reached it with no profile and no name at all. The onboarding
# gap-fill deliberately does not use it.
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


def profile_from_resume(candidate, structured: dict[str, Any] | None) -> LinkedInProfile:
    """A resume, read into the same shape the confirm step already speaks.

    So one confirm message covers both routes. The scrape and the resume answer
    the same three questions -- what do you do, where, how long -- and the
    conversation should not care which one it got; only the copy underneath
    ("got it, you're a X at Y") ever sees this, and it reads the same either
    way.

    Years come from ``Candidate.years_experience``, which the promotion has
    already computed from dated roles by the time this is called. That is a
    better number than anything on a profile: it is a union of real employment
    spans rather than a headline.
    """
    structured = structured or {}
    contact = structured.get("contact") or {}
    roles = [job for job in (structured.get("experience") or []) if isinstance(job, dict)]
    current = roles[0] if roles else {}
    years = float(getattr(candidate, "years_experience", 0.0) or 0.0)
    title = str(current.get("title") or getattr(candidate, "current_title", "") or "").strip()
    return LinkedInProfile(
        url=str(contact.get("linkedin") or getattr(candidate, "linkedin_url", "") or ""),
        full_name=str(contact.get("name") or getattr(candidate, "full_name", "") or ""),
        headline=str(structured.get("summary") or "")[:300],
        current_title=title,
        current_employer=str(
            current.get("company") or getattr(candidate, "current_company", "") or ""
        ).strip(),
        location=str(contact.get("location") or getattr(candidate, "location_city", "") or ""),
        experience_years=years or None,
        is_ic=is_ic_title(title),
    )


def apply_profile_to_record(record: dict[str, Any] | None, profile: LinkedInProfile) -> dict:
    """Fill intake fields the profile is allowed to settle.

    Seniority comes from the year count when the profile carries one, and only
    then from a management title. Asking "roughly how many years of experience
    do you have?" one message after saying "you're an investor at Westbridge,
    about 1 years in" is the agent proving it does not read its own notes --
    and it was asking because seniority was left blank for every IC, the exact
    case where the profile's own number is the answer.

    A management title still fills the gap when the profile states no tenure:
    a director is a director whatever the dates say.
    """
    incoming = empty_record()
    if profile.current_title:
        incoming["spec"]["job_titles"] = [profile.current_title]
    if profile.location:
        incoming["spec"]["locations"] = [profile.location]
    level = ""
    if isinstance(profile.experience_years, (int, float)) and profile.experience_years > 0:
        level = seniority_from_years(float(profile.experience_years))
    elif profile.current_title and not is_ic_title(profile.current_title):
        management = normalize_seniority(profile.current_title)
        if management in {"director", "vp", "cxo"}:
            level = management
    if level:
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


# "No I want to switch to Strat fin" -- a rejection, whatever follows it.
_CORRECTION = re.compile(
    r"^\W*(no|nope|nah|not\s|isn'?t|wrong|actually|instead|rather|change|switch)\b",
    re.IGNORECASE,
)

RETARGET_LINE = "got it — so what kind of role should i be looking for instead?"


def looks_like_correction(body: str) -> bool:
    """Does this reply reject what we just read back to them?

    Anchored to the start: "no, strategic finance" is a correction, while "the
    comp is not negotiable" mentions the word and agrees with the read-back.
    """
    return bool(_CORRECTION.match((body or "").strip()))


def scraped_title(thread: IntakeThread) -> str:
    return str(scraped_profile(thread).get("current_title") or "").strip()


def target_unchanged(thread: IntakeThread) -> bool:
    """Is the search target still exactly what the scrape guessed?

    The confirm step asks whether the inferred target is right. If somebody
    says no and this is still true, the extractor did not understand the
    correction -- and handing over now searches for the job they just
    rejected. Live example: "No I want to switch to Strat fin" left
    `job_titles: ["Investor"]` untouched and the thread said "you're set".
    """
    guessed = scraped_title(thread)
    if not guessed:
        return False
    titles = ((thread.record or {}).get("spec") or {}).get("job_titles") or []
    return [str(t).strip().lower() for t in titles] == [guessed.lower()]


_LEAD_IN = re.compile(
    r"^\W*(no|nope|nah|not really|actually|instead|rather|i want to|"
    r"i'd like to|i am|i'm|looking for|switch to|switching to|"
    r"change to|move to|more|into|to|do|want)\b[\s,.:;-]*",
    re.IGNORECASE,
)


def title_from_reply(body: str) -> str:
    """The reply, read as a job title. Only ever called after we asked for one.

    Deliberately light: it strips the rejection that usually opens the message
    ("no, I want to switch to strategic finance") and keeps the rest as
    written. No taxonomy mapping -- "strat fin" is what they call it, the Exa
    query is a text search, and normalising it to something we recognise is
    how the extractor lost it in the first place.
    """
    text = (body or "").strip()
    # Repeatedly: real answers stack lead-ins. "No I want to switch to Strat
    # fin" is three of them before the title, and stripping one leaves a
    # seven-word phrase the length guard then discards -- which is exactly
    # how the live thread lost this sentence.
    for _ in range(6):
        stripped = _LEAD_IN.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"\b(roles?|jobs?|positions?)\b\s*$", "", text, flags=re.IGNORECASE)
    text = text.strip(" ,.;:-")
    # A sentence is an explanation, not a title.
    if not text or len(text) > 60 or len(text.split()) > 6:
        return ""
    return text


def clear_target(record: dict[str, Any] | None) -> dict:
    """Forget the inferred target after it has been rejected.

    `merge_record` treats empty as "no news" and keeps the old value, so this
    writes the spec directly. Leaving a rejected title in place is the failure
    this whole path exists to prevent.
    """
    updated = dict(record or {})
    spec = dict(updated.get("spec") or {})
    spec["job_titles"] = []
    updated["spec"] = spec
    return updated


def apply_title_to_record(record: dict[str, Any] | None, title: str) -> dict:
    """Replace the search target outright. A correction does not accumulate."""
    incoming = empty_record()
    incoming["spec"]["job_titles"] = [title]
    return merge_record(record, incoming)


def _article(word: str) -> str:
    """"an investor", not "a investor".

    Vowel-letter test, not a pronunciation dictionary: it is right for the
    job titles that exist ("an engineer", "an analyst", "an operator") and
    wrong only for ones nobody holds.
    """
    return "an" if word[:1].lower() in "aeiou" else "a"


def confirm_message(profile: LinkedInProfile | dict[str, Any]) -> str:
    data = profile.to_dict() if isinstance(profile, LinkedInProfile) else dict(profile or {})
    bits: list[str] = []
    title = str(data.get("current_title") or "").strip()
    employer = str(data.get("current_employer") or "").strip()
    location = str(data.get("location") or "").strip()
    years = data.get("experience_years")
    if title and employer:
        bits.append(f"you're {_article(title)} {title} at {employer}")
    elif title:
        bits.append(f"you're {_article(title)} {title}")
    elif employer:
        bits.append(f"you're at {employer}")
    if location:
        bits.append(f"based in {location}")
    if isinstance(years, (int, float)) and years:
        whole = int(years)
        bits.append(f"about {whole} {'year' if whole == 1 else 'years'} in")
    if not bits:
        return (
            "got your linkedin — i'll match you off that. what kind of role are "
            "you looking for next?"
        )

    joined = ", ".join(bits)

    # State the search, not just the facts. `apply_profile_to_record` turns
    # the current title into `spec.job_titles`, which is what the job search
    # is actually run on -- so the assumption underneath is that everybody
    # wants more of the job they already have. Plenty of people are texting a
    # recruiter precisely because they do not. Reading the target back is the
    # last cheap moment to correct it: after this we spend search credits on
    # it, build a package around it, and put it in front of an employer.
    target = _target_phrase(title, location)
    if target:
        return (
            f"got it — {joined}. sounds like you're after {target} — "
            f"is that what i should be looking for, or something different?"
        )
    return (
        f"got it — {joined}. that's what we'll match you on, right? "
        f"if anything's off just say so."
    )


def _target_phrase(title: str, location: str) -> str:
    """"senior ml engineer roles in san francisco", or '' when we cannot say.

    Never guessed from an employer alone: "roles at Modal" is a different
    claim from "roles like the one you have at Modal", and the second is the
    one we mean.
    """
    role = title.strip().lower()
    if not role:
        return ""
    phrase = f"{role} roles"
    where = location.strip().lower()
    if where:
        phrase = f"{phrase} in {where}"
    return phrase


def scrape_failed_message() -> str:
    return (
        "couldn't pull your linkedin just now — no worries. "
        "mind answering a couple of quick questions so we can still match you?"
    )


def intro_message(*, recruiter: str, org: str) -> str:
    """The first thing a stranger ever reads from us.

    One number serves every tenant and the candidate cannot tell who is texting
    from the number alone, so this still names the org and discloses the AI
    before it asks for anything -- the two disclosures CANDIDATE_AGENT_PLAN §2
    is actually about.

    The STOP/HELP recital that used to sit here is gone. The argument for it was
    that a shared sender's complaint rate is what carriers score and "how do I
    make this stop" turns into a complaint when it has no answer. That is true
    of a *marketing blast*; on a first message that reads like one it is
    self-fulfilling, because an opt-out line is the single strongest cue that
    what follows is bulk. The keywords still work -- ``looks_like_stop`` and
    ``handle_stop`` are untouched -- and INTAKE_COMPLIANCE.md section 6a records
    both the reasoning and the exposure, because this trade is a judgement call
    and not a free win.
    """
    name = (recruiter or "vivian").strip() or "vivian"
    org_name = (org or "our team").strip() or "our team"
    return (
        f"hey, i'm {name} from {org_name}. i'm an ai helping the team match you "
        f"to roles — we'll ask a short series of questions so we don't waste "
        f"your time on ones that aren't a fit.\n\n"
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

    # The name is NOT asked for. It comes with the LinkedIn profile, which is
    # what the URL is for; asking anyway turns the opening of a conversation
    # into a form. When the lookup genuinely fails we would rather have an
    # unnamed candidate that a recruiter can see than a survey question.

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
