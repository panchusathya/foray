"""CRM writes: tracking a person, editing them by hand, and their notes.

Two rules live here rather than being spread across the call sites that need
them.

**What counts as tracking.** ``crm_tracked`` widens the thread-sync privacy
guard (see ``integrations/matching.py``), so it must only ever mean "someone
deliberately put this person in the CRM". There are five places that qualify —
adding to a project, enrolling in a sequence, creating a contact by hand,
promoting a discovered one, and setting a status or tag — and exactly one
function that does it, because five open-coded assignments is how one of them
ends up missing or, worse, how a stray profile edit starts retaining somebody's
mail.

**What a hand edit means.** A field a human typed is recorded in
``manual_fields`` and re-applied over imported data by ``repository._apply``.
The alternative is that the nightly refresh reverts corrections, which looks to
the user like the edit never saved.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .enrich.normalize import clean_emails, clean_phones, split_name
from .enrich.taxonomy import normalize_seniority, normalize_skills, normalize_title
from .models import Candidate, CandidateNote, CandidateTag, utcnow
from .outreach.suppressions import suppress, unsuppress
from .repository import sync_candidate_emails

# Everything a person can edit by hand. Deliberately narrow: experiences and
# educations are child rows with their own identity, and emails are the key
# every mail-matching path reads, so they get their own handling below.
EDITABLE_FIELDS = (
    "full_name",
    "first_name",
    "last_name",
    "headline",
    "summary",
    "location_city",
    "location_region",
    "location_country",
    "open_to_remote",
    "linkedin_url",
    "github_url",
    "website_url",
    "emails",
    "phones",
    "current_title",
    "current_company",
    "current_company_domain",
    "seniority",
    "crm_status",
)

# Fields whose manual value should be *merged* with imported data rather than
# replacing it. Freezing the whole list would stop Foray ever adding a newly
# discovered address, which is the opposite of what a hand edit asked for.
UNION_FIELDS = ("emails", "phones")


def track(candidate: Candidate) -> None:
    """Mark a person as deliberately in the CRM.

    Call this from the five explicit CRM actions and nowhere else. In
    particular *not* from a plain profile edit: correcting a job title must not
    quietly start retaining that person's mailbox history.
    """
    candidate.crm_tracked = True


def track_all(db: Session, *, org_id: str, candidate_ids: Iterable[str]) -> int:
    """Track a batch, org-scoped. Returns how many rows changed."""
    ids = [cid for cid in candidate_ids if cid]
    if not ids:
        return 0
    rows = db.scalars(
        select(Candidate).where(Candidate.org_id == org_id, Candidate.id.in_(ids))
    ).all()
    changed = 0
    for row in rows:
        if not row.crm_tracked:
            track(row)
            changed += 1
    return changed


def _apply_status(db: Session, candidate: Candidate, status: str) -> None:
    """Move a person's status, keeping the do-not-contact list in step.

    A ``do_not_contact`` status that the sequencer ignores is a promise the
    product breaks, so it writes a real Suppression -- which ``send_step``
    already checks on every send. Moving off it lifts the suppression again,
    but only the one this created: a bounce or an explicit opt-out is a
    different fact and is not ours to clear.
    """
    previous = candidate.crm_status
    candidate.crm_status = status
    if previous == status:
        return

    addresses = [a for a in (candidate.emails or []) if a]
    if status == "do_not_contact":
        for address in addresses:
            suppress(
                db,
                org_id=candidate.org_id,
                email=address,
                reason="manual",
                source="crm_status",
            )
    elif previous == "do_not_contact":
        for address in addresses:
            unsuppress(db, org_id=candidate.org_id, email=address)


def patch_candidate(
    db: Session,
    *,
    candidate: Candidate,
    fields: dict,
    actor_id: str | None = None,
) -> Candidate:
    """Edit a person in place.

    Deliberately *not* routed through ``repository.upsert_candidates``: its
    ``_apply`` overwrites every scalar from a normalised payload and
    ``_replace_children`` deletes and recreates experiences and educations with
    fresh ids. Editing a job title should not silently rewrite someone's
    employment history.
    """
    del actor_id  # recorded on the event, not on the row

    touched: list[str] = []
    for name, value in fields.items():
        if name not in EDITABLE_FIELDS:
            continue
        if name == "emails":
            value = clean_emails(value)
        elif name == "phones":
            value = clean_phones(value)
        elif name == "seniority":
            value = normalize_seniority(value)

        if name == "crm_status":
            _apply_status(db, candidate, value)
        else:
            setattr(candidate, name, value)
        touched.append(name)

    if not touched:
        return candidate

    # Keep the derived columns honest: a title that changed but never
    # re-normalised would still match searches for the old one.
    if "current_title" in touched:
        candidate.current_title_normalized = normalize_title(candidate.current_title)
    if "full_name" in touched and not ({"first_name", "last_name"} & set(touched)):
        candidate.first_name, candidate.last_name = split_name(candidate.full_name)
    if "skills" in touched:
        candidate.skills = normalize_skills(candidate.skills or [])

    # crm_status is a CRM action, so it tracks. Fixing a typo is not.
    if "crm_status" in touched:
        track(candidate)

    remembered = {f for f in touched if f != "crm_status"}
    if remembered:
        candidate.manual_fields = sorted(set(candidate.manual_fields or []) | remembered)

    if "emails" in touched:
        sync_candidate_emails(db, candidate)

    db.flush()
    return candidate


# --------------------------------------------------------------------------
# notes
# --------------------------------------------------------------------------


def add_note(
    db: Session,
    *,
    org_id: str,
    candidate_id: str,
    body: str,
    author_id: str | None,
    author_name: str = "",
) -> CandidateNote:
    note = CandidateNote(
        org_id=org_id,
        candidate_id=candidate_id,
        body=body,
        author_id=author_id,
        # Snapshotted so the note still says who wrote it if that user is
        # later removed.
        author_name=author_name,
    )
    db.add(note)
    db.flush()
    return note


def edit_note(db: Session, *, note: CandidateNote, body: str) -> CandidateNote:
    note.body = body
    note.edited_at = utcnow()
    db.flush()
    return note


# --------------------------------------------------------------------------
# tags
# --------------------------------------------------------------------------


def set_tags(
    db: Session,
    *,
    candidate: Candidate,
    tags: Iterable[str],
    actor_id: str | None = None,
) -> list[str]:
    """Replace a person's tags. Idempotent, and what a chip editor wants."""
    wanted = {t.strip().lower() for t in tags if t and t.strip()}
    existing = {
        row.tag: row
        for row in db.scalars(
            select(CandidateTag).where(CandidateTag.candidate_id == candidate.id)
        ).all()
    }
    for tag, row in existing.items():
        if tag not in wanted:
            db.delete(row)
    for tag in wanted - set(existing):
        db.add(
            CandidateTag(
                org_id=candidate.org_id,
                candidate_id=candidate.id,
                tag=tag,
                created_by=actor_id,
            )
        )
    if wanted:
        track(candidate)
    db.flush()
    return sorted(wanted)


def tags_for(db: Session, *, candidate_ids: list[str]) -> dict[str, list[str]]:
    """Tags for a page of people, in one query rather than one per row."""
    if not candidate_ids:
        return {}
    rows = db.scalars(
        select(CandidateTag).where(CandidateTag.candidate_id.in_(candidate_ids))
    ).all()
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row.candidate_id, []).append(row.tag)
    for tags in out.values():
        tags.sort()
    return out
