"""Finding people who are in the CRM twice, and joining them into one.

Same *address* is already impossible to duplicate: ``uq_candidate_email_org``
makes an address belong to one person, and promotion links rather than
creating. What nothing catches is the same human under two addresses --
work and personal, or a Foray import next to a hand-added contact.

**Suggestions only.** Nothing merges on its own. Auto-joining on a matching
name would fuse two real people who share one, and a merge cannot be undone
once notes, messages and enrolments have attached to the survivor. So this
proposes, ranked by how much evidence there is, and a human decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .integrations.discovery_filters import PUBLIC_EMAIL_DOMAINS, domain_of
from .models import (
    Candidate,
    CandidateEmail,
    CandidateNote,
    CandidateTag,
    DiscoveredContact,
    DuplicateDismissal,
    Education,
    Enrollment,
    Experience,
    Message,
    ProjectCandidate,
    ReplyDraft,
    SearchResult,
    utcnow,
)

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")

# Ordered strongest first; the UI shows them in this order.
CONFIDENCE = ("same_company", "work_and_personal", "name_only")


def normalise_name(name: str) -> str:
    """Fold a name for comparison. Not for display, and not stored."""
    folded = _PUNCT.sub(" ", (name or "").strip().lower())
    return _SPACES.sub(" ", folded).strip()


def _is_person_name(name: str) -> bool:
    """Two tokens minimum, and not an address.

    Single tokens match far too much -- every ``Info``, ``Support`` and
    first-name-only contact would collide with each other -- and an address is
    what an unnamed contact falls back to, so matching on it would pair people
    for having no name in common.
    """
    if not name or "@" in name:
        return False
    return len(normalise_name(name).split()) >= 2


@dataclass
class DuplicateGroup:
    name: str
    confidence: str
    reason: str
    candidate_ids: list[str] = field(default_factory=list)


def _addresses(db: Session, *, org_id: str) -> dict[str, list[str]]:
    rows = db.execute(
        select(CandidateEmail.candidate_id, CandidateEmail.email).where(
            CandidateEmail.org_id == org_id
        )
    ).all()
    out: dict[str, list[str]] = {}
    for candidate_id, email in rows:
        out.setdefault(candidate_id, []).append(email)
    return out


def _dismissed_pairs(db: Session, *, org_id: str) -> set[tuple[str, str]]:
    rows = db.execute(
        select(DuplicateDismissal.left_id, DuplicateDismissal.right_id).where(
            DuplicateDismissal.org_id == org_id
        )
    ).all()
    return {(left, right) for left, right in rows}


def _classify(people: list[Candidate], addresses: dict[str, list[str]]) -> tuple[str, str]:
    """How much reason there is to think these are one person."""
    domains = {
        (c.current_company_domain or "").strip().lower() for c in people
    } - {""}
    if len(domains) == 1:
        return "same_company", f"same name at {next(iter(domains))}"

    email_domains = {
        domain_of(a) for c in people for a in addresses.get(c.id, [])
    } - {""}
    personal = {d for d in email_domains if d in PUBLIC_EMAIL_DOMAINS}
    corporate = email_domains - personal
    if personal and corporate:
        return (
            "work_and_personal",
            f"same name on a work address and {', '.join(sorted(personal))}",
        )
    return "name_only", "same name, nothing else in common"


def suggestions(db: Session, *, org_id: str) -> list[DuplicateGroup]:
    """Groups of candidates that may be one person, strongest evidence first.

    Recomputed live rather than stored, so a group disappears the moment it is
    merged or one side is renamed. Dismissed pairs are remembered separately.
    """
    people = db.scalars(select(Candidate).where(Candidate.org_id == org_id)).all()
    addresses = _addresses(db, org_id=org_id)
    dismissed = _dismissed_pairs(db, org_id=org_id)

    by_name: dict[str, list[Candidate]] = {}
    for person in people:
        if not _is_person_name(person.full_name):
            continue
        by_name.setdefault(normalise_name(person.full_name), []).append(person)

    groups: list[DuplicateGroup] = []
    for members in by_name.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda c: c.id)
        # A group is only worth showing if at least one pair in it is
        # unresolved; dismissing "A and B" should not hide "A and C".
        live = [
            person
            for person in members
            if any(
                tuple(sorted((person.id, other.id))) not in dismissed
                for other in members
                if other.id != person.id
            )
        ]
        if len(live) < 2:
            continue
        confidence, reason = _classify(live, addresses)
        groups.append(
            DuplicateGroup(
                name=live[0].full_name,
                confidence=confidence,
                reason=reason,
                candidate_ids=[person.id for person in live],
            )
        )

    groups.sort(key=lambda g: (CONFIDENCE.index(g.confidence), g.name.lower()))
    return groups


def dismiss_pair(
    db: Session, *, org_id: str, left: str, right: str, actor_id: str | None = None
) -> None:
    """Record that two candidates are different people. Idempotent."""
    left, right = sorted((left, right))
    existing = db.scalar(
        select(DuplicateDismissal).where(
            DuplicateDismissal.left_id == left, DuplicateDismissal.right_id == right
        )
    )
    if existing is not None:
        return
    db.add(
        DuplicateDismissal(
            org_id=org_id, left_id=left, right_id=right, dismissed_by=actor_id
        )
    )
    db.flush()


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------

# Filled from the duplicate only where the survivor has nothing. The survivor
# is the record someone chose to keep; a merge should add to it, not rewrite it.
_FILLABLE = (
    "headline",
    "summary",
    "location_city",
    "location_region",
    "location_country",
    "linkedin_url",
    "github_url",
    "website_url",
    "current_title",
    "current_company",
    "current_company_domain",
)


def _move_unique(db: Session, model, *, primary_id: str, other_id: str, key) -> int:
    """Move child rows, dropping any the survivor already has.

    ``project_candidates`` and ``candidate_tags`` are unique per (parent,
    candidate). Reassigning blindly trips the constraint and takes the whole
    merge down, so the overlap is dropped instead -- the survivor's own row
    already says the same thing.

    ``enrollments`` is unique the same way but cannot use this: dropping one
    cascades to its mail. See :func:`_merge_enrollments`.
    """
    taken = {
        key(row)
        for row in db.scalars(
            select(model).where(model.candidate_id == primary_id)
        ).all()
    }
    moved = 0
    for row in db.scalars(select(model).where(model.candidate_id == other_id)).all():
        if key(row) in taken:
            db.delete(row)
            continue
        row.candidate_id = primary_id
        moved += 1
    return moved


def _merge_enrollments(db: Session, *, primary_id: str, other_id: str) -> int:
    """Move enrolments, rehoming their mail before dropping an overlap.

    ``enrollments`` is unique per (sequence, candidate), so an overlap has to
    be dropped like any other -- but ``messages.enrollment_id`` is ON DELETE
    CASCADE, and the delete would take the duplicate's sent mail with it even
    though it had just been re-pointed at the survivor. So the mail moves onto
    the survivor's own enrolment first.

    ``uq_messages_enrollment_step_outbound`` allows one outbound row per
    (enrolment, step). Where both people were sent the same step, the survivor
    already has that row, so the duplicate's copy is detached from the
    enrolment rather than dropped: the mail stays on the person's timeline,
    which is what a merge is for.
    """
    survivor_by_sequence = {
        row.sequence_id: row
        for row in db.scalars(
            select(Enrollment).where(Enrollment.candidate_id == primary_id)
        ).all()
    }
    moved = 0
    for row in db.scalars(
        select(Enrollment).where(Enrollment.candidate_id == other_id)
    ).all():
        kept = survivor_by_sequence.get(row.sequence_id)
        if kept is None:
            row.candidate_id = primary_id
            survivor_by_sequence[row.sequence_id] = row
            moved += 1
            continue

        taken = {
            message.step_id
            for message in db.scalars(
                select(Message).where(
                    Message.enrollment_id == kept.id,
                    Message.direction == "outbound",
                )
            ).all()
            if message.step_id is not None
        }
        for message in db.scalars(
            select(Message).where(Message.enrollment_id == row.id)
        ).all():
            collides = (
                message.direction == "outbound"
                and message.step_id is not None
                and message.step_id in taken
            )
            message.enrollment_id = None if collides else kept.id
            if not collides and message.step_id is not None:
                taken.add(message.step_id)
        # The rehomed rows must be written before the cascade can see them.
        db.flush()
        db.delete(row)
    return moved


def merge(
    db: Session, *, org_id: str, primary_id: str, duplicate_ids: list[str]
) -> dict:
    """Fold duplicates into ``primary_id`` and delete them.

    Every table that references a candidate is handled explicitly rather than
    left to cascade: a cascade would delete the duplicate's mail and notes
    along with the row, which is the opposite of merging.
    """
    primary = db.scalar(
        select(Candidate).where(Candidate.id == primary_id, Candidate.org_id == org_id)
    )
    if primary is None:
        return {"merged": 0, "error": "primary not found"}

    others = [
        person
        for person in db.scalars(
            select(Candidate).where(
                Candidate.org_id == org_id,
                Candidate.id.in_([i for i in duplicate_ids if i != primary_id]),
            )
        ).all()
    ]
    if not others:
        return {"merged": 0, "error": "nothing to merge"}

    moved = {"messages": 0, "notes": 0, "emails": 0, "projects": 0, "enrollments": 0}
    for other in others:
        # Mail and notes have no uniqueness to worry about and are the whole
        # point of merging: the survivor must end up with the full history.
        for row in db.scalars(
            select(Message).where(Message.candidate_id == other.id)
        ).all():
            row.candidate_id = primary.id
            moved["messages"] += 1
        for row in db.scalars(
            select(CandidateNote).where(CandidateNote.candidate_id == other.id)
        ).all():
            row.candidate_id = primary.id
            moved["notes"] += 1
        for row in db.scalars(
            select(SearchResult).where(SearchResult.candidate_id == other.id)
        ).all():
            row.candidate_id = primary.id
        for row in db.scalars(
            select(ReplyDraft).where(ReplyDraft.candidate_id == other.id)
        ).all():
            row.candidate_id = primary.id
        for row in db.scalars(
            select(DiscoveredContact).where(DiscoveredContact.candidate_id == other.id)
        ).all():
            row.candidate_id = primary.id

        moved["projects"] += _move_unique(
            db, ProjectCandidate, primary_id=primary.id, other_id=other.id,
            key=lambda row: row.project_id,
        )
        moved["enrollments"] += _merge_enrollments(
            db, primary_id=primary.id, other_id=other.id
        )
        _move_unique(
            db, CandidateTag, primary_id=primary.id, other_id=other.id,
            key=lambda row: row.tag,
        )

        # Employment history belongs to a profile rather than being additive:
        # two imports of the same person would otherwise stack duplicate roles.
        # Only taken when the survivor has none.
        if not primary.experiences:
            for row in db.scalars(
                select(Experience).where(Experience.candidate_id == other.id)
            ).all():
                row.candidate_id = primary.id
        if not primary.educations:
            for row in db.scalars(
                select(Education).where(Education.candidate_id == other.id)
            ).all():
                row.candidate_id = primary.id

        # Addresses union; the projection is rebuilt from the column afterwards.
        merged_emails = list(primary.emails or [])
        for address in other.emails or []:
            if address not in merged_emails:
                merged_emails.append(address)
                moved["emails"] += 1
        primary.emails = merged_emails
        for row in db.scalars(
            select(CandidateEmail).where(CandidateEmail.candidate_id == other.id)
        ).all():
            db.delete(row)

        for name in _FILLABLE:
            if not getattr(primary, name, "") and getattr(other, name, ""):
                setattr(primary, name, getattr(other, name))
        primary.skills = sorted({*(primary.skills or []), *(other.skills or [])})
        primary.manual_fields = sorted(
            {*(primary.manual_fields or []), *(other.manual_fields or [])}
        )
        primary.crm_tracked = primary.crm_tracked or other.crm_tracked
        if primary.crm_status in ("", "none") and other.crm_status not in ("", "none"):
            primary.crm_status = other.crm_status

        # A pair that was dismissed and then merged anyway leaves a stale row
        # pointing at a candidate about to vanish.
        for row in db.scalars(
            select(DuplicateDismissal).where(
                or_(
                    DuplicateDismissal.left_id == other.id,
                    DuplicateDismissal.right_id == other.id,
                )
            )
        ).all():
            db.delete(row)

        db.flush()
        db.delete(other)

    db.flush()

    from .repository import sync_candidate_emails, sync_candidate_phones
    from .search.index import invalidate

    sync_candidate_emails(db, primary)
    # The loser's rows went with them on delete; the survivor now owns the
    # union of both phone lists and needs the numbers that just freed up.
    sync_candidate_phones(db, primary)
    invalidate(org_id)
    primary.updated_at = utcnow()
    db.flush()
    return {"merged": len(others), "primary_id": primary.id, "moved": moved, "error": ""}


# --------------------------------------------------------------------------
# "is this person already a contact?" -- asked by the CSV importer, before
# anything is written
# --------------------------------------------------------------------------
#
# Different question from ``suggestions`` above, which asks it of the CRM
# against itself and proposes merges. This one is asked of *inbound rows* that
# are not records yet, so the recruiter can drop them in the review table
# rather than discover the duplicate afterwards.
#
# ``upsert_candidates`` already dedupes on ``(org_id, source, source_id)``, so
# re-uploading the same export merges cleanly. What it cannot see is the same
# human under a different ``source``: a Foray-sourced profile, a hand-added
# contact, or somebody who rang the intake line all carry a different key, and
# a CSV row for them creates a second row. ``uq_candidate_email_org`` looks
# like it would stop that, but ``sync_candidate_emails`` *skips* an address
# another candidate already owns rather than raising -- so the duplicate lands
# with the address in its ``emails`` JSON and no projection row at all.

# Ordered strongest first, like CONFIDENCE. The UI shows the strongest match.
MATCHED_ON = ("email", "linkedin", "name_and_company")

# How many IN-clause values to send at once. SQLite's default parameter
# ceiling is what forces this, not the planner: a 5000-row file carries more
# addresses than a single statement may bind.
_IN_CHUNK = 400

# LinkedIn is stored as it arrived -- ``normalize_candidate`` only strips it --
# so a profile can be on file under any of a dozen spellings and there is no
# indexed slug to join on. The match is therefore one projected scan, folded
# in Python. Bounded, because "load every candidate into a dict" is fine once
# per sync job and not fine inside a request; past the cap LinkedIn stops
# contributing and ``truncated`` says so out loud rather than letting a big
# workspace look clean.
MAX_LINKEDIN_SCAN = 50_000


@dataclass(frozen=True)
class Identity:
    """One inbound row: everything we can use to recognise who it is."""

    key: str
    emails: tuple[str, ...] = ()
    linkedin_url: str = ""
    full_name: str = ""
    company: str = ""


@dataclass(frozen=True)
class KnownMatch:
    key: str
    candidate_id: str
    full_name: str
    matched_on: str
    reason: str


@dataclass(frozen=True)
class KnownResult:
    matches: tuple[KnownMatch, ...] = ()
    # True when the LinkedIn scan hit MAX_LINKEDIN_SCAN, so the answer is
    # "these are duplicates" and never "these are all of them".
    truncated: bool = False


@dataclass(frozen=True)
class _Folded:
    """An Identity with every field normalised the way the DB stores it."""

    key: str
    emails: tuple[str, ...]
    slug: str
    name: str
    company: str


def _batched(values: list[str], size: int = _IN_CHUNK):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _fold(identities: list[Identity]) -> list[_Folded]:
    from .enrich.linkedin_url import linkedin_slug
    from .enrich.normalize import clean_emails
    from .enrich.taxonomy import normalize_company

    out: list[_Folded] = []
    for identity in identities:
        # The slug is computed here, server-side, and never taken from the
        # client: the browser's linkedinSlug does not rstrip "._-" the way
        # enrich.linkedin_url does, and a slug folded two ways silently misses.
        out.append(
            _Folded(
                key=identity.key,
                emails=tuple(clean_emails(list(identity.emails))),
                slug=linkedin_slug(identity.linkedin_url),
                # Gated on _is_person_name: a single token matches every
                # "Info" and first-name-only contact in the workspace, and an
                # address-as-name would pair people for having no name at all.
                name=(
                    normalise_name(identity.full_name)
                    if _is_person_name(identity.full_name)
                    else ""
                ),
                company=normalize_company(identity.company),
            )
        )
    return out


def known_contacts(db: Session, *, org_id: str, identities: list[Identity]) -> KnownResult:
    """Which of these inbound rows are already contacts, and on what evidence.

    Reads only. Set-based regardless of how many rows arrive:
    ``repository.owner_of_email`` is a point lookup and ``owner_of_linkedin``
    is a LIKE scan, so calling either per row would be hundreds of round
    trips over one import. Their *predicates* are reused; the functions are
    not.
    """
    folded = _fold(identities)
    if not folded:
        return KnownResult()

    truncated = False
    # key -> (rank in MATCHED_ON, candidate_id, name, matched_on, reason).
    # Strongest wins, so email beats LinkedIn beats name-and-company.
    best: dict[str, tuple[int, str, str, str, str]] = {}

    def offer(key: str, candidate_id: str, name: str, matched_on: str, reason: str) -> None:
        rank = MATCHED_ON.index(matched_on)
        current = best.get(key)
        if current is None or rank < current[0]:
            best[key] = (rank, candidate_id, name, matched_on, reason)

    # 1. Address. The same indexed projection owner_of_email reads, and the one
    #    create_candidate's 409 already treats as authoritative.
    wanted_emails = sorted({address for row in folded for address in row.emails})
    owners: dict[str, tuple[str, str]] = {}
    for batch in _batched(wanted_emails):
        for address, candidate_id, name in db.execute(
            select(CandidateEmail.email, CandidateEmail.candidate_id, Candidate.full_name)
            .join(Candidate, Candidate.id == CandidateEmail.candidate_id)
            .where(CandidateEmail.org_id == org_id, CandidateEmail.email.in_(batch))
        ).all():
            owners[address] = (candidate_id, name)
    for row in folded:
        for address in row.emails:
            hit = owners.get(address)
            if hit:
                offer(row.key, hit[0], hit[1], "email", f"same email: {address}")
                break

    # 2. LinkedIn. One projected scan; see MAX_LINKEDIN_SCAN.
    wanted_slugs = {row.slug for row in folded if row.slug}
    if wanted_slugs:
        from .enrich.linkedin_url import linkedin_slug

        by_slug: dict[str, tuple[str, str]] = {}
        # One over the cap, so a full page is distinguishable from a truncated one.
        rows = db.execute(
            select(Candidate.id, Candidate.full_name, Candidate.linkedin_url)
            .where(Candidate.org_id == org_id, Candidate.linkedin_url != "")
            .limit(MAX_LINKEDIN_SCAN + 1)
        ).all()
        truncated = len(rows) > MAX_LINKEDIN_SCAN
        for candidate_id, name, url in rows[:MAX_LINKEDIN_SCAN]:
            slug = linkedin_slug(url)
            if slug and slug in wanted_slugs and slug not in by_slug:
                by_slug[slug] = (candidate_id, name)
        for row in folded:
            hit = by_slug.get(row.slug) if row.slug else None
            if hit:
                offer(row.key, hit[0], hit[1], "linkedin", "same LinkedIn profile")

    # 3. Name at the same company. current_company_normalized is indexed, so
    #    this is the one match that filters in SQL; the names are folded in
    #    Python with the same normalise_name suggestions() uses.
    wanted_companies = sorted({row.company for row in folded if row.company and row.name})
    if wanted_companies:
        by_name_company: dict[tuple[str, str], tuple[str, str]] = {}
        for batch in _batched(wanted_companies):
            for candidate_id, name, company in db.execute(
                select(
                    Candidate.id,
                    Candidate.full_name,
                    Candidate.current_company_normalized,
                ).where(
                    Candidate.org_id == org_id,
                    Candidate.current_company_normalized.in_(batch),
                )
            ).all():
                if not _is_person_name(name):
                    continue
                by_name_company.setdefault((normalise_name(name), company), (candidate_id, name))
        for row in folded:
            if not (row.name and row.company):
                continue
            hit = by_name_company.get((row.name, row.company))
            if hit:
                offer(row.key, hit[0], hit[1], "name_and_company", f"same name at {row.company}")

    matches = tuple(
        KnownMatch(
            key=key,
            candidate_id=candidate_id,
            full_name=name,
            matched_on=matched_on,
            reason=reason,
        )
        for key, (_, candidate_id, name, matched_on, reason) in sorted(best.items())
    )
    return KnownResult(matches=matches, truncated=truncated)
