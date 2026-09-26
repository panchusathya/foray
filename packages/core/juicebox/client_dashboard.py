"""Client dashboards: the people we have shared with a hiring company.

A client (e.g. Flowgen) logs in to one page listing everybody we submitted,
moves them through its own interview stages and leaves notes; we see the same
rows from the recruiter app and can do the same. Both routers go through this
module, so the two sides cannot drift on what a stage move records or who a
note is attributed to.

Non-obvious rules:

- **The client is served the snapshot, never the contact.** Profile fields
  are copied onto ``ClientSubmission`` when a person is shared, and
  ``client_row`` builds its output from that row alone. Nothing here loads a
  ``Candidate`` on the client's behalf, so an address, a phone, comp or a CRM
  note has no path to a client even if the recruiter shape grows.
- **A stage move always writes an event, from either side.** The event is the
  audit of who moved whom, and the "changed by Flowgen" signal on our side.
  Setting a stage to the one it already holds writes nothing -- otherwise a
  re-pasted list would bury real moves under no-ops.
- **Re-sharing somebody updates, it does not duplicate.** ``dedupe_key``
  folds on LinkedIn, then contact id, then name, and an existing row only has
  its *blank* snapshot fields filled: the client may already be reading the
  version we shared first.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .enrich.linkedin_url import canonical_linkedin_url, linkedin_slug
from .models import (
    CLIENT_STAGES,
    Candidate,
    ClientAccount,
    ClientSubmission,
    ClientSubmissionEvent,
    ClientSubmissionNote,
    ClientUser,
    Project,
    ProjectCandidate,
    utcnow,
)
from .repository import owner_of_linkedin
from .schemas import (
    ClientEntriesIn,
    ClientEntryIn,
    ClientEntryResult,
    ClientEventOut,
    ClientNoteOut,
    ClientSubmissionOut,
)
from .security import hash_password

ACTOR_CLIENT = "client"
ACTOR_RECRUITER = "recruiter"

_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")


class ClientDashboardError(ValueError):
    """A request the caller can fix; routers turn it into a 4xx."""


@dataclass(frozen=True)
class Actor:
    kind: str  # ACTOR_CLIENT | ACTOR_RECRUITER
    id: str | None
    name: str


# --------------------------------------------------------------------------
# accounts and logins
# --------------------------------------------------------------------------


def normalize_username(value: str) -> str:
    return (value or "").strip().lower()


def validate_stage(stage: str) -> str:
    if stage not in CLIENT_STAGES:
        raise ClientDashboardError(
            f"unknown stage {stage!r}; expected one of {', '.join(CLIENT_STAGES)}"
        )
    return stage


def create_login(
    db: Session, *, account: ClientAccount, username: str, password: str, name: str = ""
) -> ClientUser:
    key = normalize_username(username)
    # No "@" allowed, and that is load-bearing: the login form sends anything
    # with an "@" to the recruiter login and anything without to this one.
    if not _USERNAME.match(key):
        raise ClientDashboardError(
            "username must be 3-80 characters: letters, digits, dot, dash or underscore"
        )
    if db.scalar(select(ClientUser.id).where(ClientUser.username == key)):
        raise ClientDashboardError(f"username {key!r} is taken")
    user = ClientUser(
        org_id=account.org_id,
        client_account_id=account.id,
        username=key,
        name=name.strip() or account.name,
        password_hash=hash_password(password),
    )
    db.add(user)
    db.flush()
    return user


def load_account(db: Session, *, org_id: str, account_id: str) -> ClientAccount | None:
    return db.scalar(
        select(ClientAccount).where(ClientAccount.id == account_id, ClientAccount.org_id == org_id)
    )


def load_submission(
    db: Session, *, org_id: str, submission_id: str, account_id: str | None = None
) -> ClientSubmission | None:
    query = select(ClientSubmission).where(
        ClientSubmission.id == submission_id, ClientSubmission.org_id == org_id
    )
    if account_id is not None:
        query = query.where(ClientSubmission.client_account_id == account_id)
    return db.scalar(query)


# --------------------------------------------------------------------------
# sharing people
# --------------------------------------------------------------------------


def _fold_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def dedupe_key(*, linkedin_url: str, candidate_id: str | None, full_name: str) -> str:
    slug = linkedin_slug(linkedin_url)
    if slug:
        return f"li:{slug}"
    if candidate_id:
        return f"cand:{candidate_id}"
    folded = _fold_name(full_name)
    return f"name:{folded}" if folded else ""


def _candidate_location(candidate: Candidate) -> str:
    parts = [candidate.location_city, candidate.location_region, candidate.location_country]
    return ", ".join(p for p in parts if p)


def _snapshot(entry: ClientEntryIn, candidate: Candidate | None) -> dict:
    """What the client will see. The pasted value wins over the contact's."""
    linkedin = canonical_linkedin_url(entry.linkedin_url) or entry.linkedin_url.strip()
    snap = {
        "full_name": entry.full_name.strip(),
        "linkedin_url": linkedin,
        "current_title": entry.current_title.strip(),
        "current_company": entry.current_company.strip(),
        "location": "",
    }
    if candidate is not None:
        snap["full_name"] = snap["full_name"] or candidate.full_name
        snap["linkedin_url"] = snap["linkedin_url"] or candidate.linkedin_url
        snap["current_title"] = snap["current_title"] or candidate.current_title
        snap["current_company"] = snap["current_company"] or candidate.current_company
        snap["location"] = _candidate_location(candidate)
    if not snap["full_name"] and snap["linkedin_url"]:
        # A bare URL still needs something to read in a grid; the slug is the
        # person's own choice of handle, which beats "Unknown".
        slug = linkedin_slug(snap["linkedin_url"])
        snap["full_name"] = " ".join(w.capitalize() for w in re.split(r"[-_]+", slug) if w)
    return snap


def _resolve_candidate(db: Session, *, org_id: str, entry: ClientEntryIn) -> Candidate | None:
    if entry.candidate_id:
        candidate = db.get(Candidate, entry.candidate_id)
        if candidate is None or candidate.org_id != org_id:
            raise ClientDashboardError(f"contact {entry.candidate_id} not found")
        return candidate
    if entry.linkedin_url:
        owner_id = owner_of_linkedin(db, org_id=org_id, url=entry.linkedin_url)
        if owner_id:
            return db.get(Candidate, owner_id)
    return None


def share_entries(
    db: Session, *, account: ClientAccount, payload: ClientEntriesIn, actor: Actor
) -> list[ClientEntryResult]:
    entries = list(payload.entries)
    if payload.project_id:
        project = db.get(Project, payload.project_id)
        if project is None or project.org_id != account.org_id:
            raise ClientDashboardError("project not found")
        member_ids = db.scalars(
            select(ProjectCandidate.candidate_id)
            .where(ProjectCandidate.project_id == project.id)
            .order_by(ProjectCandidate.created_at)
        ).all()
        entries.extend(ClientEntryIn(candidate_id=cid) for cid in member_ids)

    default_stage = validate_stage(payload.stage) if payload.stage else ""
    results: list[ClientEntryResult] = []
    for line, entry in enumerate(entries, start=1):
        stage = entry.stage or default_stage
        if stage:
            validate_stage(stage)
        candidate = _resolve_candidate(db, org_id=account.org_id, entry=entry)
        snap = _snapshot(entry, candidate)
        key = dedupe_key(
            linkedin_url=snap["linkedin_url"],
            candidate_id=candidate.id if candidate else None,
            full_name=snap["full_name"],
        )
        if not key:
            results.append(
                ClientEntryResult(line=line, outcome="skipped", reason="no name or LinkedIn URL")
            )
            continue

        match = [ClientSubmission.dedupe_key == key]
        if candidate is not None:
            match.append(ClientSubmission.candidate_id == candidate.id)
        existing = db.scalar(
            select(ClientSubmission)
            .where(ClientSubmission.client_account_id == account.id, or_(*match))
            .limit(1)
        )
        role_title = entry.role_title.strip() or payload.role_title.strip()

        if existing is None:
            row = ClientSubmission(
                org_id=account.org_id,
                client_account_id=account.id,
                candidate_id=candidate.id if candidate else None,
                dedupe_key=key,
                role_title=role_title,
                stage=stage or "in_process",
                stage_changed_by_kind=actor.kind,
                stage_changed_by_name=actor.name,
                **snap,
            )
            db.add(row)
            db.flush()
            db.add(
                ClientSubmissionEvent(
                    org_id=row.org_id,
                    submission_id=row.id,
                    from_stage="",
                    to_stage=row.stage,
                    actor_kind=actor.kind,
                    actor_name=actor.name,
                )
            )
            results.append(
                ClientEntryResult(
                    line=line,
                    outcome="added",
                    submission_id=row.id,
                    full_name=row.full_name,
                    matched_contact=candidate is not None,
                )
            )
            continue

        changed = False
        for field, value in snap.items():
            if value and not getattr(existing, field):
                setattr(existing, field, value)
                changed = True
        if candidate is not None and existing.candidate_id is None:
            existing.candidate_id = candidate.id
            changed = True
        if role_title and role_title != existing.role_title:
            existing.role_title = role_title
            changed = True
        if stage and move_stage(db, submission=existing, stage=stage, actor=actor):
            changed = True
        results.append(
            ClientEntryResult(
                line=line,
                outcome="updated" if changed else "unchanged",
                submission_id=existing.id,
                full_name=existing.full_name,
                matched_contact=existing.candidate_id is not None,
            )
        )
    db.flush()
    return results


# --------------------------------------------------------------------------
# stages and notes
# --------------------------------------------------------------------------


def move_stage(db: Session, *, submission: ClientSubmission, stage: str, actor: Actor) -> bool:
    validate_stage(stage)
    if submission.stage == stage:
        return False
    db.add(
        ClientSubmissionEvent(
            org_id=submission.org_id,
            submission_id=submission.id,
            from_stage=submission.stage,
            to_stage=stage,
            actor_kind=actor.kind,
            actor_name=actor.name,
        )
    )
    submission.stage = stage
    submission.stage_changed_at = utcnow()
    submission.stage_changed_by_kind = actor.kind
    submission.stage_changed_by_name = actor.name
    db.flush()
    return True


def add_note(
    db: Session, *, submission: ClientSubmission, body: str, actor: Actor
) -> ClientSubmissionNote:
    text = body.strip()
    if not text:
        raise ClientDashboardError("a note cannot be empty")
    note = ClientSubmissionNote(
        org_id=submission.org_id,
        submission_id=submission.id,
        author_kind=actor.kind,
        author_id=actor.id,
        author_name=actor.name,
        body=text,
    )
    db.add(note)
    # Touch the row so "last updated" on either side reflects the note.
    submission.updated_at = utcnow()
    db.flush()
    return note


def note_out(note: ClientSubmissionNote, actor: Actor) -> ClientNoteOut:
    return ClientNoteOut(
        id=note.id,
        author_kind=note.author_kind,
        author_name=note.author_name,
        body=note.body,
        created_at=note.created_at,
        mine=note.author_kind == actor.kind and note.author_id == actor.id,
    )


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def _latest_notes(db: Session, submission_ids: list[str]) -> dict[str, ClientSubmissionNote]:
    if not submission_ids:
        return {}
    latest: dict[str, ClientSubmissionNote] = {}
    rows = db.scalars(
        select(ClientSubmissionNote)
        .where(ClientSubmissionNote.submission_id.in_(submission_ids))
        .order_by(ClientSubmissionNote.created_at)
    ).all()
    for note in rows:
        latest[note.submission_id] = note  # ascending, so the last write wins
    return latest


def _note_counts(db: Session, submission_ids: list[str]) -> dict[str, int]:
    if not submission_ids:
        return {}
    return dict(
        db.execute(
            select(ClientSubmissionNote.submission_id, func.count(ClientSubmissionNote.id))
            .where(ClientSubmissionNote.submission_id.in_(submission_ids))
            .group_by(ClientSubmissionNote.submission_id)
        ).all()
    )


def list_submissions(db: Session, *, account_id: str, org_id: str) -> list[ClientSubmission]:
    return list(
        db.scalars(
            select(ClientSubmission)
            .where(
                ClientSubmission.client_account_id == account_id,
                ClientSubmission.org_id == org_id,
            )
            .order_by(ClientSubmission.shared_at.desc(), ClientSubmission.full_name)
        ).all()
    )


def client_rows(
    db: Session, rows: list[ClientSubmission], actor: Actor, *, model=ClientSubmissionOut
) -> list:
    """Serialise rows as ``model``. Reads the submission, nothing else.

    ``model`` is ``ClientSubmissionOut`` for a client and the recruiter subclass
    for us. Only the recruiter model declares ``candidate_id``, so a client
    response cannot carry it even though the attribute exists on the row.
    """
    ids = [r.id for r in rows]
    counts = _note_counts(db, ids)
    latest = _latest_notes(db, ids)
    fields = set(model.model_fields) - {"note_count", "latest_note", "notes", "events"}
    out = []
    for row in rows:
        note = latest.get(row.id)
        out.append(
            model(
                **{f: getattr(row, f) for f in fields},
                note_count=counts.get(row.id, 0),
                latest_note=note_out(note, actor) if note else None,
            )
        )
    return out


def detail(db: Session, row: ClientSubmission, actor: Actor, *, model) -> object:
    notes = db.scalars(
        select(ClientSubmissionNote)
        .where(ClientSubmissionNote.submission_id == row.id)
        .order_by(ClientSubmissionNote.created_at)
    ).all()
    events = db.scalars(
        select(ClientSubmissionEvent)
        .where(ClientSubmissionEvent.submission_id == row.id)
        .order_by(ClientSubmissionEvent.created_at)
    ).all()
    fields = set(model.model_fields) - {"note_count", "latest_note", "notes", "events"}
    return model(
        **{f: getattr(row, f) for f in fields},
        note_count=len(notes),
        latest_note=note_out(notes[-1], actor) if notes else None,
        notes=[note_out(n, actor) for n in notes],
        events=[
            ClientEventOut(
                from_stage=e.from_stage,
                to_stage=e.to_stage,
                actor_kind=e.actor_kind,
                actor_name=e.actor_name,
                created_at=e.created_at,
            )
            for e in events
        ],
    )


def stage_counts(db: Session, *, account_id: str) -> dict[str, int]:
    counts = dict.fromkeys(CLIENT_STAGES, 0)
    for stage, n in db.execute(
        select(ClientSubmission.stage, func.count(ClientSubmission.id))
        .where(ClientSubmission.client_account_id == account_id)
        .group_by(ClientSubmission.stage)
    ).all():
        counts[stage] = n
    return counts


def client_activity(db: Session, *, account_id: str, days: int = 7) -> int:
    since = utcnow() - timedelta(days=days)
    moves = db.scalar(
        select(func.count(ClientSubmissionEvent.id))
        .join(ClientSubmission, ClientSubmission.id == ClientSubmissionEvent.submission_id)
        .where(
            ClientSubmission.client_account_id == account_id,
            ClientSubmissionEvent.actor_kind == ACTOR_CLIENT,
            ClientSubmissionEvent.created_at >= since,
        )
    )
    notes = db.scalar(
        select(func.count(ClientSubmissionNote.id))
        .join(ClientSubmission, ClientSubmission.id == ClientSubmissionNote.submission_id)
        .where(
            ClientSubmission.client_account_id == account_id,
            ClientSubmissionNote.author_kind == ACTOR_CLIENT,
            ClientSubmissionNote.created_at >= since,
        )
    )
    return int(moves or 0) + int(notes or 0)
