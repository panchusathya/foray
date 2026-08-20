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

    ``project_candidates``, ``candidate_tags`` and ``enrollments`` are unique
    per (parent, candidate). Reassigning blindly trips the constraint and takes
    the whole merge down, so the overlap is dropped instead -- the survivor's
    own row already says the same thing.
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
        moved["enrollments"] += _move_unique(
            db, Enrollment, primary_id=primary.id, other_id=other.id,
            key=lambda row: row.sequence_id,
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

    from .repository import sync_candidate_emails
    from .search.index import invalidate

    sync_candidate_emails(db, primary)
    invalidate(org_id)
    primary.updated_at = utcnow()
    db.flush()
    return {"merged": len(others), "primary_id": primary.id, "moved": moved, "error": ""}
