"""Operational CLI: ``juicebox <command>``.

    juicebox init-db
    juicebox migrate [revision] [--down]
    juicebox seed [--count 120]
    juicebox import <file.jsonl> --org <org_id>
    juicebox search "senior ML engineers in SF" --org <org_id>
    juicebox run-outreach --org <org_id> [--dry-run]
    juicebox run-agents --org <org_id>
    juicebox job-card <posting_id> [--candidate <id>] [--out <dir>]
    juicebox job-card --sample [--out <dir>]
    juicebox work [--once]
    juicebox backfill-jobs [--org]
    juicebox reembed --org <org_id>
    juicebox export-seed <file.jsonl> [--count 120]
    juicebox purge-placeholder-emails [--apply]
    juicebox serve [--port 8000]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import func, select

from .db import session_scope
from .migrate import ensure_schema
from .models import Org
from .repository import reembed_org, upsert_candidates


def _resolve_org(db, org: str | None) -> str:
    if org:
        found = db.get(Org, org) or db.scalar(select(Org).where(Org.slug == org))
        if found is None:
            raise SystemExit(f"org not found: {org}")
        return found.id
    orgs = db.scalars(select(Org).order_by(Org.created_at)).all()
    if not orgs:
        raise SystemExit("no orgs yet — run `juicebox seed` first")
    if len(orgs) > 1:
        names = ", ".join(f"{o.slug}({o.id})" for o in orgs)
        raise SystemExit(f"multiple orgs, pass --org: {names}")
    return orgs[0].id


def _read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"skipping line {line_number}: {exc}", file=sys.stderr)
    return records


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_init_db(_args: argparse.Namespace) -> int:
    from .migrate import current_revision

    how = ensure_schema()
    print(f"schema ready via {how} (revision {current_revision()})")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from . import migrate

    if args.down:
        migrate.downgrade(args.revision)
    else:
        migrate.upgrade(args.revision)
    print(f"database at revision {migrate.current_revision()}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from .seed import seed_demo

    ensure_schema()
    with session_scope() as db:
        info = seed_demo(db, candidate_count=args.count)
    print(json.dumps(info, indent=2))
    print(f"\nlog in with {info['user_email']} / {info['password']}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"file not found: {path}")
    records = _read_jsonl(path)
    ensure_schema()
    with session_scope() as db:
        org_id = _resolve_org(db, args.org)
        result = upsert_candidates(db, org_id=org_id, payloads=records)
    print(f"created={result.created} updated={result.updated}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .search.engine import get_engine

    with session_scope() as db:
        org_id = _resolve_org(db, args.org)
        outcome = get_engine().search(
            db,
            org_id=org_id,
            query=args.query,
            limit=args.limit,
            persist=False,
            use_rerank=args.rerank,
        )
        print(
            f"parser={outcome.parser} reranked={outcome.reranked} "
            f"matched={outcome.total_matched} in {outcome.latency_ms}ms\n"
        )
        for hit in outcome.results:
            doc = hit.doc
            print(
                f"{hit.rank:>3}. {doc.full_name:<24} {hit.final_score:.3f}  "
                f"{doc.current_title} @ {doc.current_company}"
            )
            if args.verbose:
                for criterion in hit.criteria:
                    mark = {"strong": "+", "partial": "~", "miss": "-"}.get(criterion.status, "?")
                    print(f"       {mark} {criterion.label}: {criterion.evidence}")
    return 0


def cmd_run_outreach(args: argparse.Namespace) -> int:
    from .outreach.sequencer import run_due

    with session_scope() as db:
        org_id = _resolve_org(db, args.org)
        summary = run_due(db, org_id=org_id, dry_run=args.dry_run)
    print(
        f"processed={summary.processed} sent={summary.sent} failed={summary.failed} "
        f"completed={summary.completed} skipped={summary.skipped}"
    )
    return 0


def cmd_ats_credential(args: argparse.Namespace) -> int:
    """Store, list or remove an employer's job-board API key.

    No settings screen yet, and these are rare: a key arrives when a client
    whose roles we are filling sets one up, not per deployment.
    """
    from sqlalchemy import select

    from .applications.credentials import set_board_key
    from .models import AtsBoardCredential

    with session_scope() as db:
        org_id = _resolve_org(db, args.org)
        if args.list:
            rows = db.scalars(
                select(AtsBoardCredential)
                .where(AtsBoardCredential.org_id == org_id)
                .order_by(AtsBoardCredential.vendor, AtsBoardCredential.tenant)
            ).all()
            if not rows:
                print("no board keys stored; every posting goes through the hosted form")
            for row in rows:
                used = row.last_used_at.isoformat() if row.last_used_at else "never"
                print(f"{row.vendor:16} {row.tenant:24} {row.status:8} last used {used}")
            return 0
        if not (args.vendor and args.tenant):
            print("--vendor and --tenant are required", file=sys.stderr)
            return 2
        if args.remove:
            row = db.scalar(
                select(AtsBoardCredential).where(
                    AtsBoardCredential.org_id == org_id,
                    AtsBoardCredential.vendor == args.vendor,
                    AtsBoardCredential.tenant == args.tenant,
                )
            )
            if row is None:
                print("no such credential")
                return 1
            db.delete(row)
            print(f"removed the {args.vendor} key for {args.tenant}")
            return 0
        key = args.key or os.environ.get("ATS_BOARD_KEY", "")
        if not key:
            # Never as an argv value if it can be helped: argv is visible in
            # `ps` and lands in shell history.
            print("pass --key, or put it in ATS_BOARD_KEY", file=sys.stderr)
            return 2
        set_board_key(
            db,
            org_id=org_id,
            vendor=args.vendor,
            tenant=args.tenant,
            key=key,
            label=args.label,
        )
        print(f"stored a {args.vendor} key for {args.tenant}; that board now uses the API")
    return 0


def cmd_ats_probe(args: argparse.Namespace) -> int:
    """Check a live posting's form against the field map we would fill.

    Navigates and counts. Never fills, never submits -- running it costs the
    employer one page view. This is the maintenance tool for the browser tier:
    when a vendor reshapes their form, this is what says which selector died.
    """
    from .applications.submit.browser.probe import probe

    failed = 0
    for url in args.urls:
        report = probe(url, org_id=args.org or "probe")
        print(report.render())
        print()
        if not report.usable:
            failed += 1
    return 1 if failed else 0


def _placeholder_mark(colour: str) -> bytes:
    """A stand-in logo for `job-card --sample`. Never used for a real card."""
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (256, 256), (255, 255, 255, 0))
    ImageDraw.Draw(image).ellipse((32, 32, 224, 224), fill=colour or "#4cc38a")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def cmd_job_card(args: argparse.Namespace) -> int:
    """Render one posting's card to a PNG so a person can look at it.

    The maintenance tool for the card the way ``ats-probe`` is for the browser
    tier. A card is generated art over data we did not choose -- an employer's
    logo, a palette quantised out of it -- so "does this look right" is a
    question only an eye answers, and a test asserting contrast ratios cannot.

    ``--sample`` needs no database and no posting: it renders the branded, pale
    and neutral variants side by side, which is what to look at after touching
    the layout.
    """
    from .presentation.cardimage import palette_for, render_card_png

    out = Path(args.out or ".")
    out.mkdir(parents=True, exist_ok=True)

    if args.sample:
        from .enrich.brand import Brand

        card = {
            "company": "Pepsico",
            "title": "Director, Strategic Finance",
            "location": "Purchase, NY (remote ok)",
            "seniority": "Director",
            "source_label": "open market",
            "reasons": [
                "12 years in CPG finance, 6 leading a team",
                "Ran the FP&A rebuild they are hiring for",
            ],
        }
        variants = [
            ("branded", Brand(domain="pepsico.com", primary="#004b93", accent="#e32934")),
            ("pale", Brand(domain="snap.com", primary="#fffc00")),
            ("neutral", None),
        ]
        for name, brand in variants:
            path = out / f"card-{name}.png"
            # A placeholder mark, because the logo tile shifts the identity row
            # -- a sample without one shows a layout no real card has.
            path.write_bytes(
                render_card_png(
                    card,
                    brand=brand,
                    logo=_placeholder_mark(brand.primary if brand else "#4cc38a"),
                    index=2,
                    total=3,
                )
            )
            palette = palette_for(brand)
            print(f"{path}  ground={palette.ground} ink={palette.ink} accent={palette.accent}")
        return 0

    from . import storage
    from .enrich import brand as brandmod
    from .models import JobMatch, JobPosting
    from .presentation.jobcard import jobcard

    with session_scope() as db:
        posting = db.get(JobPosting, args.posting_id)
        if posting is None:
            print(f"no posting {args.posting_id}")
            return 1
        match = None
        if args.candidate:
            match = db.scalar(
                select(JobMatch).where(
                    JobMatch.job_posting_id == posting.id,
                    JobMatch.candidate_id == args.candidate,
                )
            )
        card = {k: v for k, v in jobcard(posting, match).items() if k != "recruiter_only"}
        brand = brandmod.resolve(db, posting, refresh=args.refresh_brand)
        logo = b""
        if brand is not None and brand.logo_path:
            logo = storage.read_bytes(brand.logo_path)
        path = out / f"card-{posting.id}.png"
        path.write_bytes(render_card_png(card, brand=brand, logo=logo))
        palette = palette_for(brand)
        print(f"{path}")
        print(f"  brand={brand.domain if brand else '(none)'} source={brand.source if brand else '-'}")
        print(f"  ground={palette.ground} ink={palette.ink} accent={palette.accent}")
    return 0


def cmd_run_agents(args: argparse.Namespace) -> int:
    from .agents.sourcing import run_due_agents

    with session_scope() as db:
        org_id = _resolve_org(db, args.org)
        runs = run_due_agents(db, org_id=org_id)
    if not runs:
        print("no agents due")
    for run in runs:
        print(f"{run.agent_id}: evaluated={run.evaluated} added={run.added} enrolled={run.enrolled}")
    return 0


def cmd_work(args: argparse.Namespace) -> int:
    from .jobs import INTERACTIVE_KINDS
    from .worker import drain, run_loop

    if args.once:
        with session_scope() as db:
            processed = drain(db)
        print(f"processed={processed}")
        return 0
    run_loop(only=INTERACTIVE_KINDS if args.intake_only else None)
    return 0


def cmd_backfill_jobs(args: argparse.Namespace) -> int:
    from .jobmatch.sync import backfill_project_postings

    ensure_schema()
    with session_scope() as db:
        org_id = _resolve_org(db, args.org) if args.org else None
        count = backfill_project_postings(db, org_id=org_id)
    print(f"upserted {count} project postings")
    return 0


def cmd_reembed(args: argparse.Namespace) -> int:
    with session_scope() as db:
        org_id = _resolve_org(db, args.org)
        count = reembed_org(db, org_id=org_id)
    print(f"re-embedded {count} candidates (and any job postings)")
    return 0


def cmd_export_seed(args: argparse.Namespace) -> int:
    from .seed import generate_candidates

    path = Path(args.file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in generate_candidates(args.count):
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"wrote {args.count} candidates to {path}")
    return 0


def cmd_purge_placeholder_emails(args: argparse.Namespace) -> int:
    """Strip filler addresses out of candidates that already have them.

    Cleanup for a provider incident: ContactOut answered HTTP 200 with a sample
    profile once its quota ran out, so email1@example.com / email2@gmail.com
    reached real candidate records. Goes through patch_candidate rather than
    writing the column directly, so the candidate_emails projection and the
    search index stay consistent -- the projection is a subset of the JSON and
    drifts silently otherwise.

    Defaults to a dry run. Nothing is written without --apply.
    """
    from .enrich.normalize import strip_placeholders
    from .models import Candidate
    from .repository import sync_candidate_emails
    from .search.index import invalidate

    changed = 0
    removed = 0
    with session_scope() as db:
        candidates = db.scalars(select(Candidate)).all()
        touched_orgs = set()
        for candidate in candidates:
            keep, drop = strip_placeholders(candidate.emails)
            if not drop:
                continue
            changed += 1
            removed += len(drop)
            print(
                f"{'apply' if args.apply else 'would drop'}  {candidate.id}  "
                f"{candidate.full_name}: {', '.join(drop)}"
                + (f"  (keeping {', '.join(keep)})" if keep else "  (leaving no address)")
            )
            if args.apply:
                candidate.emails = keep
                # The projection is a strict subset of this column and drifts
                # silently if it is not rebuilt. Deliberately NOT going through
                # patch_candidate: that records the field as a manual edit, which
                # would block a later real enrichment from filling the gap.
                sync_candidate_emails(db, candidate)
                touched_orgs.add(candidate.org_id)
        if args.apply:
            for org_id in touched_orgs:
                invalidate(org_id)

    verb = "removed" if args.apply else "would remove"
    print(f"\n{verb} {removed} address(es) from {changed} candidate(s)")
    if not args.apply and changed:
        print("re-run with --apply to write the change")
    return 0


def cmd_intake_number(args: argparse.Namespace) -> int:
    """Register a phone number against an org, or list what is registered.

    Without a row here every inbound call is refused with a 404, because the
    webhook resolves the org from the number that was dialled and refuses to
    guess. This is the one step that cannot be scripted from the provider side.
    """
    from .intake.phones import lookup_key
    from .models import IntakeNumber

    with session_scope() as db:
        if args.list:
            rows = db.scalars(select(IntakeNumber)).all()
            if not rows:
                print("no numbers registered")
                return 0
            for row in rows:
                state = "active" if row.active else "disabled"
                print(
                    f"{row.phone}  org={row.org_id}  {row.provider}  {state}  "
                    f"in={row.inbound_agent_id or '-'}  out={row.outbound_agent_id or '-'}"
                )
            return 0

        key = lookup_key(args.number)
        if not key:
            print(
                f"cannot normalise {args.number!r} to E.164. Give the full "
                "international form, e.g. +16283865454."
            )
            return 2

        org_id = _resolve_org(db, args.org)
        existing = db.scalar(select(IntakeNumber).where(IntakeNumber.phone == key))
        if existing is not None and existing.org_id != org_id:
            # One number, one org. Reassigning silently would hand another
            # workspace's calls -- and their transcripts -- to this one.
            print(
                f"{key} is already registered to org {existing.org_id}. "
                "Numbers belong to exactly one org; remove it there first."
            )
            return 2

        row = existing or IntakeNumber(org_id=org_id, phone=key)
        row.provider = args.provider
        row.label = args.label or row.label
        row.active = not args.disable
        if args.inbound_agent:
            row.inbound_agent_id = args.inbound_agent
        if args.outbound_agent:
            row.outbound_agent_id = args.outbound_agent
        db.add(row)
        db.flush()
        verb = "updated" if existing is not None else "registered"
        print(f"{verb} {key} -> org {org_id} ({'active' if row.active else 'disabled'})")
    return 0


def cmd_reset_thread(args: argparse.Namespace) -> int:
    """Wipe one person's conversation so onboarding can be tested from zero.

    Testing the intake flow means being a stranger again, and a thread that
    already knows your name resumes instead of greeting you. This removes the
    conversation and, unless told otherwise, the candidate row it created --
    which cascades to their preference, matches, proposals, applications and
    documents.

    **Dry run by default.** It prints what it would delete and stops; --apply
    writes. Deleting somebody's record is not a thing to do on a typo in a
    phone number.

    A do-not-contact suppression is left alone unless --clear-suppression is
    passed. If you texted STOP during the test that row is why the next
    message is refused, and removing it is a decision worth typing out --
    everywhere else in this codebase an opt-out is permanent.
    """
    from .intake.phones import lookup_key
    from .models import (
        Application,
        Candidate,
        CandidateDocument,
        CandidatePreference,
        ConsentRecord,
        IntakeMessage,
        IntakeThread,
        JobMatch,
        JobProposal,
        Suppression,
    )

    phone = lookup_key(args.phone) or args.phone.strip()
    if not phone:
        print("give a phone number, e.g. --phone +16462010859")
        return 2

    with session_scope() as db:
        threads = list(
            db.scalars(select(IntakeThread).where(IntakeThread.from_address == phone)).all()
        )
        suppression = db.scalar(select(Suppression).where(Suppression.phone == phone))
        if not threads and not (suppression is not None and args.clear_suppression):
            print(f"no intake thread for {phone}")
            if suppression is not None:
                # Worth saying out loud: with a suppression standing, a fresh
                # thread here is still refused at the first send, and "nothing
                # to reset" would read as "you are good to go".
                print(
                    f"  a do-not-contact row ({suppression.reason}) is still in place; "
                    "the next message would be refused"
                )
                print("  pass --clear-suppression to remove it")
            return 0

        candidate_ids = {t.candidate_id for t in threads if t.candidate_id}
        thread_ids = [t.id for t in threads]

        def _count(model, column, values):
            if not values:
                return 0
            return int(
                db.scalar(select(func.count()).select_from(model).where(column.in_(values))) or 0
            )

        print(f"{phone}")
        print(f"  threads          {len(threads)}")
        print(f"  messages         {_count(IntakeMessage, IntakeMessage.thread_id, thread_ids)}")
        print(f"  consent records  {_count(ConsentRecord, ConsentRecord.thread_id, thread_ids)}")
        if candidate_ids:
            print(f"  candidates       {len(candidate_ids)}")
            for model, column, label in (
                (CandidatePreference, CandidatePreference.candidate_id, "preferences"),
                (JobMatch, JobMatch.candidate_id, "job matches"),
                (JobProposal, JobProposal.candidate_id, "proposals"),
                (Application, Application.candidate_id, "applications"),
                (CandidateDocument, CandidateDocument.candidate_id, "documents"),
            ):
                print(f"  {label:<16} {_count(model, column, candidate_ids)}")

        if suppression is not None:
            state = "will clear" if args.clear_suppression else "LEFT IN PLACE"
            print(f"  suppression      1 ({suppression.reason}) -- {state}")
            if not args.clear_suppression:
                print("    the next message to this number will be refused;")
                print("    pass --clear-suppression to remove it")

        if not args.apply:
            print("\ndry run. re-run with --apply to delete")
            return 0

        # Threads first: a consent record points at both, and dropping the
        # candidate out from under it would SET NULL the half we still want to
        # find rows by.
        for thread in threads:
            db.delete(thread)
        db.flush()

        if not args.keep_candidate:
            for candidate_id in candidate_ids:
                candidate = db.get(Candidate, candidate_id)
                if candidate is not None:
                    db.delete(candidate)

        if suppression is not None and args.clear_suppression:
            db.delete(suppression)

        db.flush()
        kept = " (candidate kept)" if args.keep_candidate else ""
        print(f"\ndeleted{kept}. text the number again and onboarding starts from the intro.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "juicebox.main:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="juicebox", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="bring the schema to head").set_defaults(func=cmd_init_db)

    migrate_cmd = sub.add_parser("migrate", help="run database migrations")
    migrate_cmd.add_argument("revision", nargs="?", default="head")
    migrate_cmd.add_argument(
        "--down", action="store_true", help="downgrade to the given revision instead"
    )
    migrate_cmd.set_defaults(func=cmd_migrate)

    seed = sub.add_parser("seed", help="create the demo workspace")
    seed.add_argument("--count", type=int, default=120)
    seed.set_defaults(func=cmd_seed)

    imp = sub.add_parser("import", help="import candidates from a .jsonl file")
    imp.add_argument("file")
    imp.add_argument("--org", default=None)
    imp.set_defaults(func=cmd_import)

    search = sub.add_parser("search", help="run a natural-language search")
    search.add_argument("query")
    search.add_argument("--org", default=None)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--verbose", action="store_true")
    search.add_argument(
        "--rerank",
        dest="rerank",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="force LLM reranking on or off",
    )
    search.set_defaults(func=cmd_search)

    outreach = sub.add_parser("run-outreach", help="send every due sequence step")
    outreach.add_argument("--org", default=None)
    outreach.add_argument("--dry-run", action="store_true")
    outreach.set_defaults(func=cmd_run_outreach)

    agents = sub.add_parser("run-agents", help="run every due sourcing agent")
    agents.add_argument("--org", default=None)
    agents.set_defaults(func=cmd_run_agents)

    ats_credential = sub.add_parser(
        "ats-credential", help="store an employer's job-board API key"
    )
    ats_credential.add_argument("--org", default="")
    ats_credential.add_argument("--vendor", default="", help="greenhouse | lever | ashby")
    ats_credential.add_argument("--tenant", default="", help="board token / site / org")
    ats_credential.add_argument("--key", default="", help="or set ATS_BOARD_KEY")
    ats_credential.add_argument("--label", default="")
    ats_credential.add_argument("--remove", action="store_true")
    ats_credential.add_argument("--list", action="store_true")
    ats_credential.set_defaults(func=cmd_ats_credential)

    ats_probe = sub.add_parser(
        "ats-probe", help="check a live posting's form against our field map (read-only)"
    )
    ats_probe.add_argument("urls", nargs="+")
    ats_probe.add_argument("--org", default="")
    ats_probe.set_defaults(func=cmd_ats_probe)

    job_card = sub.add_parser(
        "job-card", help="render a posting's card image to a PNG so a person can look at it"
    )
    job_card.add_argument("posting_id", nargs="?", default="")
    job_card.add_argument("--candidate", default="", help="include this candidate's match reasons")
    job_card.add_argument("--out", default="", help="directory to write into (default: cwd)")
    job_card.add_argument("--refresh-brand", action="store_true", help="refetch the logo")
    job_card.add_argument(
        "--sample", action="store_true", help="render the three variants; no database needed"
    )
    job_card.set_defaults(func=cmd_job_card)

    intake_number = sub.add_parser(
        "intake-number", help="register a voice intake phone number against an org"
    )
    intake_number.add_argument("number", nargs="?", default="", help="E.164, e.g. +16283865454")
    intake_number.add_argument("--org", default="")
    intake_number.add_argument("--provider", default="retell")
    intake_number.add_argument("--label", default="")
    intake_number.add_argument("--inbound-agent", default="")
    intake_number.add_argument("--outbound-agent", default="")
    intake_number.add_argument("--disable", action="store_true", help="keep the row, stop taking calls")
    intake_number.add_argument("--list", action="store_true", help="show registered numbers")
    intake_number.set_defaults(func=cmd_intake_number)

    work = sub.add_parser("work", help="claim and run jobs from the durable queue")
    work.add_argument(
        "--once",
        action="store_true",
        help="drain due work and exit (used by make worker)",
    )
    work.add_argument(
        "--intake-only",
        action="store_true",
        help=(
            "run ONLY the kinds a texter is waiting on. Give these their own "
            "process: a batch of job parses can hold the general worker for "
            "minutes, and nothing inside a process can preempt work already "
            "running -- measured at 284s to answer 'Hey' on a live thread"
        ),
    )
    work.set_defaults(func=cmd_work)

    backfill = sub.add_parser(
        "backfill-jobs", help="upsert a JobPosting for every Project mandate"
    )
    backfill.add_argument("--org", default=None)
    backfill.set_defaults(func=cmd_backfill_jobs)

    reembed = sub.add_parser("reembed", help="recompute candidate embeddings")
    reembed.add_argument("--org", default=None)
    reembed.set_defaults(func=cmd_reembed)

    export = sub.add_parser("export-seed", help="write the generated corpus to jsonl")
    export.add_argument("file")
    export.add_argument("--count", type=int, default=120)
    export.set_defaults(func=cmd_export_seed)

    purge = sub.add_parser(
        "purge-placeholder-emails",
        help="strip filler addresses (email1@example.com and friends) from candidates",
    )
    purge.add_argument(
        "--apply", action="store_true", help="write the change (default is a dry run)"
    )
    purge.set_defaults(func=cmd_purge_placeholder_emails)

    reset = sub.add_parser(
        "reset-thread", help="wipe one person's conversation so onboarding restarts"
    )
    reset.add_argument("--phone", required=True, help="E.164 number, e.g. +16462010859")
    reset.add_argument(
        "--apply", action="store_true", help="actually delete (default is a dry run)"
    )
    reset.add_argument(
        "--keep-candidate",
        action="store_true",
        help="drop the conversation but leave the CRM record and its history",
    )
    reset.add_argument(
        "--clear-suppression",
        action="store_true",
        help="also remove a do-not-contact row, if the test ended in STOP",
    )
    reset.set_defaults(func=cmd_reset_thread)

    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    return parser


def _configure_logging() -> None:
    """Give the CLI the same handler `main.py` gives the API.

    Without this the worker is silent. `logging.basicConfig` only ran in
    `main.py`, which uvicorn imports and the CLI does not -- so every
    `logger.info` in the worker process went to a root logger with no handler
    and was dropped. In production that means the queue loop, `[console-apply]`,
    the alias-mail router and every "sending as" line write to nothing, and the
    only visible symptom is a container with empty logs.

    LOG_LEVEL overrides for a noisy debugging session.
    """
    import logging
    import os

    if logging.getLogger().handlers:
        return
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
