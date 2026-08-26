"""Applying to a role the candidate found themselves.

Two things are being pinned, and they are separate on purpose.

The **gate** -- name, email, resume -- is a property of the candidate, so it
does not expire and answering it once serves every later application. The
**step budget** is what stops the agent nagging: one message too many gets a
number blocked and every candidate after that silently receives nothing, so a
fourth ask is a worse outcome than a request that waits.

And the thing that must stay true throughout: nothing here submits. The flow
ends at ``create_application`` + ``request_package``, so every guardrail past
that point is reached exactly as it was before.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.jobmatch.fromurl import (
    SOURCE_CANDIDATE_LINK,
    ReadPosting,
    read_posting,
    upsert_from_url,
    urls_in,
)
from juicebox.messaging import applylink, intents, prompts
from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.manager import MODE_MANAGING, run_manager_turn
from juicebox.messaging.threads import record_inbound
from juicebox.models import (
    Application,
    Candidate,
    CandidateDocument,
    IntakeThread,
    JobPosting,
)

POSTING_URL = "https://boards.greenhouse.io/ramp/jobs/4321"

JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Head of Finance",
 "description":"<p>We are hiring a Head of Finance to own FP&amp;A.</p>",
 "hiringOrganization":{"@type":"Organization","name":"Ramp","url":"https://ramp.com"},
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
   "addressLocality":"New York","addressRegion":"NY"}}}
</script>
</head><body></body></html>
"""

TITLE_ONLY_PAGE = (
    "<html><head><title>Head of Finance at Ramp</title>"
    '<meta property="og:site_name" content="Ramp"></head><body>hiring</body></html>'
)

UNREADABLE_PAGE = "<html><head><title>Careers</title></head><body>jobs</body></html>"


class _Response:
    def __init__(self, text, *, url, status_code=200):
        self.text = text
        self.url = url
        self.status_code = status_code


class _Client:
    """Answers one URL with one page. Never touches the network."""

    def __init__(self, page, *, status_code=200):
        self.page = page
        self.status_code = status_code
        self.calls: list[str] = []

    def get(self, url):
        self.calls.append(url)
        return _Response(self.page, url=url, status_code=self.status_code)

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Reading the page
# ---------------------------------------------------------------------------


class TestReadingAPastedLink:
    def test_jsonld_gives_the_title_company_and_location(self):
        read = read_posting(POSTING_URL, client=_Client(JSONLD_PAGE))
        assert read is not None
        assert read.title == "Head of Finance"
        assert read.company == "Ramp"
        assert read.location == "New York, NY"
        assert read.company_domain == "ramp.com"
        assert "FP&A" in read.jd_text

    def test_a_page_title_is_the_fallback(self):
        read = read_posting(POSTING_URL, client=_Client(TITLE_ONLY_PAGE))
        assert read is not None
        assert (read.title, read.company) == ("Head of Finance", "Ramp")

    def test_a_page_we_cannot_read_yields_nothing_rather_than_half(self):
        """A row with a title and no employer would address an application to
        nobody, and the confirm message is only a real check if both halves
        came off the page."""
        assert read_posting(POSTING_URL, client=_Client(UNREADABLE_PAGE)) is None

    def test_a_404_yields_nothing(self):
        assert read_posting(POSTING_URL, client=_Client("", status_code=404)) is None

    def test_a_non_http_string_is_not_fetched(self):
        client = _Client(JSONLD_PAGE)
        assert read_posting("tell me about this job", client=client) is None
        assert client.calls == []

    def test_remote_is_read_off_the_location_type(self):
        page = JSONLD_PAGE.replace(
            '"jobLocation"', '"jobLocationType":"TELECOMMUTE","jobLocation"'
        )
        read = read_posting(POSTING_URL, client=_Client(page))
        assert read.remote is True

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("apply to https://x.test/jobs/1 for me", ["https://x.test/jobs/1"]),
            ("apply to https://x.test/jobs/1, thanks", ["https://x.test/jobs/1"]),
            ("https://x.test/a and https://x.test/b", ["https://x.test/a", "https://x.test/b"]),
            ("no link here", []),
        ],
    )
    def test_urls_are_found_and_punctuation_trimmed(self, message, expected):
        assert urls_in(message) == expected


class TestUpsert:
    def test_it_creates_one_posting_with_the_candidate_link_provenance(
        self, session, account
    ):
        read = ReadPosting(
            url=POSTING_URL, title="Head of Finance", company="Ramp", location="New York, NY"
        )
        posting = upsert_from_url(
            session, org_id=account["org_id"], read=read, source_url=POSTING_URL
        )
        assert posting.source == SOURCE_CANDIDATE_LINK
        assert posting.status == "open"
        assert posting.company_normalized

    def test_the_same_link_twice_is_one_row(self, session, account):
        read = ReadPosting(url=POSTING_URL, title="Head of Finance", company="Ramp")
        first = upsert_from_url(
            session, org_id=account["org_id"], read=read, source_url=POSTING_URL
        )
        second = upsert_from_url(
            session, org_id=account["org_id"], read=read, source_url=POSTING_URL
        )
        assert first.id == second.id

    def test_it_does_not_overwrite_a_curated_row(self, session, account):
        """A candidate's link to a role a recruiter already imported should add
        the URL, not replace a curated title with a scraped one."""
        existing = JobPosting(
            org_id=account["org_id"],
            source="project",
            source_id="x",
            company="Ramp",
            title="Head of Finance, Americas",
            status="open",
        )
        session.add(existing)
        session.flush()
        from juicebox.jobmatch.ids import source_id_for

        existing.source_id = source_id_for(url=POSTING_URL)
        session.flush()

        read = ReadPosting(url=POSTING_URL, title="Head of Finance", company="Ramp")
        posting = upsert_from_url(
            session, org_id=account["org_id"], read=read, source_url=POSTING_URL
        )
        assert posting.id == existing.id
        assert posting.title == "Head of Finance, Americas"


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------


class TestIntent:
    def test_apply_words_plus_a_link_carry_the_url(self):
        intent = intents.classify(f"can you apply to {POSTING_URL} for me", presented=[])
        assert intent.kind == intents.INTENT_APPLY
        assert intent.url == POSTING_URL

    def test_a_link_with_no_apply_words_is_not_an_application(self):
        """Somebody sharing a posting to ask about it is the commoner case, and
        acting on the wrong reading costs an application."""
        intent = intents.classify(f"what do you think of {POSTING_URL}", presented=[])
        assert intent.kind != intents.INTENT_APPLY or not intent.url

    def test_apply_to_an_ordinal_carries_no_url(self):
        intent = intents.classify("apply to 2", presented=[{"company": "Ramp"}, {"company": "X"}])
        assert intent.kind == intents.INTENT_APPLY
        assert intent.url == ""
        assert intent.selection == 2

    def test_our_own_portal_link_is_never_read_as_a_job(self, monkeypatch):
        """A candidate quoting the link we just sent them is not pasting a job,
        and ingesting it would make a posting out of our own page."""
        from juicebox.config import get_settings

        portal = get_settings().portal_base_url.rstrip("/")
        assert applylink.link_in(f"apply via {portal}/m/abc123") == ""


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _managing_thread(session, org_id, *, candidate_id=None, name="", email=""):
    thread = IntakeThread(
        org_id=org_id,
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-apply",
        from_address="+14155550123",
        to_address="+14155559001",
        consent_messaging=True,
        mode=MODE_MANAGING,
        candidate_id=candidate_id,
        contact_name=name,
        contact_email=email,
        last_service="iMessage",
    )
    session.add(thread)
    session.flush()
    return thread


def _say(session, thread, body):
    message, _ = record_inbound(session, thread=thread, body=body)
    session.flush()
    return message


@pytest.fixture
def channel():
    channel = FakeChannel()
    set_channel(LINQ, channel)
    return channel


@pytest.fixture
def no_network(monkeypatch):
    """The pasted page, without a socket. Also holds the card image off: this
    module is about the apply flow, not about rendering."""
    monkeypatch.setattr(
        "juicebox.messaging.applylink.read_posting",
        lambda url, client=None: ReadPosting(
            url=url,
            title="Head of Finance",
            company="Ramp",
            location="New York, NY",
            company_domain="ramp.com",
            jd_text="We are hiring a Head of Finance.",
        ),
    )
    monkeypatch.setattr(
        "juicebox.applications.ats.detect.resolve_target",
        lambda db, posting, **kw: None,
    )
    monkeypatch.setattr(
        "juicebox.messaging.manager.card_media", lambda db, **kw: ()
    )


class TestTheGate:
    def test_a_candidate_with_nothing_on_file_needs_email_and_name(self, session, account):
        thread = _managing_thread(session, account["org_id"])
        assert applylink.missing(session, thread=thread, candidate=None) == (
            applylink.NEED_EMAIL,
            applylink.NEED_NAME,
        )

    def test_an_uploaded_resume_closes_the_resume_half(self, session, account):
        candidate = Candidate(
            org_id=account["org_id"],
            source="manual",
            source_id="c1",
            full_name="Priya Raman",
            emails=["priya@example.com"],
        )
        session.add(candidate)
        session.flush()
        thread = _managing_thread(session, account["org_id"], candidate_id=candidate.id)
        assert applylink.missing(session, thread=thread, candidate=candidate) == (
            applylink.NEED_RESUME,
        )

        session.add(
            CandidateDocument(
                org_id=account["org_id"],
                candidate_id=candidate.id,
                kind="resume_uploaded",
                filename="r.pdf",
                status="ready",
                is_default=True,
            )
        )
        session.flush()
        assert applylink.missing(session, thread=thread, candidate=candidate) == ()

    def test_employment_history_counts_as_a_resume(self, session, account):
        """`service.build_package` pays for the full history when a candidate
        has a LinkedIn URL, so this is a real resume rather than a loophole."""
        from juicebox.models import Experience

        candidate = Candidate(
            org_id=account["org_id"],
            source="manual",
            source_id="c2",
            full_name="Priya Raman",
            emails=["priya@example.com"],
            linkedin_url="https://linkedin.com/in/priya",
        )
        session.add(candidate)
        session.flush()
        session.add(
            Experience(candidate_id=candidate.id, company="Stripe", title="Controller")
        )
        session.flush()
        session.refresh(candidate)
        thread = _managing_thread(session, account["org_id"], candidate_id=candidate.id)
        assert applylink.missing(session, thread=thread, candidate=candidate) == ()


# ---------------------------------------------------------------------------
# The conversation
# ---------------------------------------------------------------------------


def _ready_candidate(session, org_id, *, with_resume=True):
    candidate = Candidate(
        org_id=org_id,
        source="manual",
        source_id="ready",
        full_name="Priya Raman",
        emails=["priya@example.com"],
    )
    session.add(candidate)
    session.flush()
    if with_resume:
        session.add(
            CandidateDocument(
                org_id=org_id,
                candidate_id=candidate.id,
                kind="resume_uploaded",
                filename="r.pdf",
                status="ready",
                is_default=True,
            )
        )
        session.flush()
    return candidate


class TestTheFlow:
    def test_a_pasted_link_is_confirmed_before_anything_is_created(
        self, session, account, channel, no_network
    ):
        """The confirm is not politeness. It is the only check that we read the
        page right, and applying to the wrong role is not recoverable."""
        thread = _managing_thread(session, account["org_id"])
        message = _say(session, thread, f"apply to {POSTING_URL} for me")
        result = run_manager_turn(session, thread=thread, last=message)

        assert result.action == "asked"
        assert channel.bodies == [
            prompts.APPLY_LINK_CONFIRM.format(title="Head of Finance", company="Ramp")
        ]
        assert session.scalars(select(Application)).all() == []
        assert applylink.pending(thread).status == applylink.STATUS_CONFIRMING

    def test_an_unreadable_link_asks_rather_than_guessing(
        self, session, account, channel, monkeypatch
    ):
        monkeypatch.setattr(
            "juicebox.messaging.applylink.read_posting", lambda url, client=None: None
        )
        thread = _managing_thread(session, account["org_id"])
        message = _say(session, thread, f"apply to {POSTING_URL} for me")
        run_manager_turn(session, thread=thread, last=message)

        assert channel.bodies == [prompts.lowercase_outbound(prompts.APPLY_LINK_UNREADABLE)]
        assert session.scalars(select(JobPosting)).all() == []

    def test_the_gate_is_asked_one_item_per_message(
        self, session, account, channel, no_network
    ):
        thread = _managing_thread(session, account["org_id"])
        run_manager_turn(
            session, thread=thread, last=_say(session, thread, f"apply to {POSTING_URL}")
        )
        run_manager_turn(session, thread=thread, last=_say(session, thread, "yes please"))
        assert channel.bodies[-1] == prompts.lowercase_outbound(prompts.APPLY_LINK_NEED_EMAIL)
        assert session.scalars(select(Application)).all() == []

        run_manager_turn(
            session, thread=thread, last=_say(session, thread, "priya@example.com")
        )
        assert channel.bodies[-1] == prompts.lowercase_outbound(prompts.APPLY_LINK_NEED_NAME)
        assert session.scalars(select(Application)).all() == []

    def test_a_complete_profile_applies_on_the_confirm(
        self, session, account, channel, no_network
    ):
        candidate = _ready_candidate(session, account["org_id"])
        thread = _managing_thread(
            session, account["org_id"], candidate_id=candidate.id, name="Priya Raman"
        )
        run_manager_turn(
            session, thread=thread, last=_say(session, thread, f"apply to {POSTING_URL}")
        )
        result = run_manager_turn(
            session, thread=thread, last=_say(session, thread, "yes go for it")
        )

        assert result.action == "applied"
        application = session.scalars(select(Application)).one()
        assert application.source == "candidate"
        # The per-application yes, bound to the message it arrived in.
        assert application.consenting_message_id
        assert "yes go for it" in application.consent_verbatim
        assert applylink.pending(thread).status == applylink.STATUS_DONE

    def test_declining_the_confirm_creates_nothing_and_clears_it(
        self, session, account, channel, no_network
    ):
        candidate = _ready_candidate(session, account["org_id"])
        thread = _managing_thread(
            session, account["org_id"], candidate_id=candidate.id, name="Priya"
        )
        run_manager_turn(
            session, thread=thread, last=_say(session, thread, f"apply to {POSTING_URL}")
        )
        run_manager_turn(session, thread=thread, last=_say(session, thread, "no"))
        assert session.scalars(select(Application)).all() == []
        assert not applylink.pending(thread)

    def test_a_duplicate_request_is_you_are_already_in_flight(
        self, session, account, channel, no_network
    ):
        candidate = _ready_candidate(session, account["org_id"])
        thread = _managing_thread(
            session, account["org_id"], candidate_id=candidate.id, name="Priya"
        )
        for _ in range(2):
            run_manager_turn(
                session, thread=thread, last=_say(session, thread, f"apply to {POSTING_URL}")
            )
            run_manager_turn(session, thread=thread, last=_say(session, thread, "yes"))
        # One application per posting, ever -- the database index, not a check
        # this flow reimplemented.
        assert len(session.scalars(select(Application)).all()) == 1
        assert "already in flight" in channel.bodies[-1].lower()

    def test_the_step_budget_parks_rather_than_asking_again(
        self, session, account, channel, no_network
    ):
        """One message too many gets a number blocked, and every candidate
        after that silently receives nothing."""
        thread = _managing_thread(session, account["org_id"])
        run_manager_turn(
            session, thread=thread, last=_say(session, thread, f"apply to {POSTING_URL}")
        )
        for reply in ("yes", "priya@example.com", "Priya Raman", "ok", "still here", "hello"):
            run_manager_turn(session, thread=thread, last=_say(session, thread, reply))

        wanted = {
            prompts.lowercase_outbound(prompts.APPLY_LINK_NEED_EMAIL),
            prompts.lowercase_outbound(prompts.APPLY_LINK_NEED_NAME),
        }
        asks = [body for body in channel.bodies if body in wanted]
        assert len(asks) <= 2
        parked = [b for b in channel.bodies if "hold onto that role" in b.lower()]
        # Parked exactly once, however many turns follow.
        assert len(parked) == 1
        assert applylink.pending(thread).status == applylink.STATUS_PARKED

    def test_a_parked_request_still_answers_a_status_question(
        self, session, account, channel, no_network
    ):
        """`_advance_apply_link` returning None is what hands the turn back to
        the intent router, so a parked request does not swallow the thread."""
        thread = _managing_thread(session, account["org_id"])
        run_manager_turn(
            session, thread=thread, last=_say(session, thread, f"apply to {POSTING_URL}")
        )
        applylink.remember(thread, status=applylink.STATUS_PARKED, steps=applylink.MAX_STEPS)
        session.flush()
        before = len(channel.bodies)
        result = run_manager_turn(
            session, thread=thread, last=_say(session, thread, "what's my status")
        )
        assert result.action == "answered"
        assert len(channel.bodies) == before + 1
        assert "No applications in flight" in channel.bodies[-1] or channel.bodies[-1]

    def test_a_resume_upload_wakes_the_parked_request(self, client, session, account):
        """The file arrives through the upload route, not the thread, so no
        inbound message would otherwise trigger a turn -- the candidate uploads
        a resume and nothing happens."""
        from juicebox.jobs import KIND_INTAKE_TURN, Job
        from juicebox.messaging.uploads import mint_upload_link

        candidate = _ready_candidate(session, account["org_id"], with_resume=False)
        thread = _managing_thread(
            session, account["org_id"], candidate_id=candidate.id, name="Priya"
        )
        applylink.remember(
            thread,
            url=POSTING_URL,
            posting_id="jpost_x",
            status=applylink.STATUS_WAITING_RESUME,
            steps=3,
        )
        session.commit()

        link = mint_upload_link(thread)
        token = link.split("token=")[1]
        response = client.post(
            f"/v1/intake/uploads/{thread.id}?token={token}",
            files={"file": ("r.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert response.status_code == 201
        woken = [
            job
            for job in session.scalars(select(Job)).all()
            if job.kind == KIND_INTAKE_TURN and job.payload.get("thread_id") == thread.id
        ]
        assert woken, "a parked apply-link request must be woken by the upload"

    def test_an_upload_with_no_parked_request_wakes_nothing(
        self, client, session, account
    ):
        from juicebox.jobs import KIND_INTAKE_TURN, Job
        from juicebox.messaging.uploads import mint_upload_link

        candidate = _ready_candidate(session, account["org_id"], with_resume=False)
        thread = _managing_thread(
            session, account["org_id"], candidate_id=candidate.id, name="Priya"
        )
        session.commit()
        token = mint_upload_link(thread).split("token=")[1]
        client.post(
            f"/v1/intake/uploads/{thread.id}?token={token}",
            files={"file": ("r.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        turns = [
            job for job in session.scalars(select(Job)).all() if job.kind == KIND_INTAKE_TURN
        ]
        assert turns == []


class TestNothingHereSubmits:
    def test_the_flow_ends_at_a_queued_package_build(
        self, session, account, channel, no_network
    ):
        """A second door into submission is what applications/service.py exists
        to prevent. This one ends where `_apply` ends."""
        from juicebox.jobs import KIND_APP_PACKAGE, KIND_APP_SUBMIT, Job

        candidate = _ready_candidate(session, account["org_id"])
        thread = _managing_thread(
            session, account["org_id"], candidate_id=candidate.id, name="Priya"
        )
        run_manager_turn(
            session, thread=thread, last=_say(session, thread, f"apply to {POSTING_URL}")
        )
        run_manager_turn(session, thread=thread, last=_say(session, thread, "yes"))

        kinds = {job.kind for job in session.scalars(select(Job)).all()}
        assert KIND_APP_PACKAGE in kinds
        assert KIND_APP_SUBMIT not in kinds
        assert session.scalars(select(Application)).one().status == "draft"

    def test_the_bar_is_not_consulted(self, session, account, channel, no_network):
        """The bar decides what *we* propose. Telling somebody their own choice
        scored too low is not a product."""
        candidate = _ready_candidate(session, account["org_id"])
        thread = _managing_thread(
            session, account["org_id"], candidate_id=candidate.id, name="Priya"
        )
        run_manager_turn(
            session, thread=thread, last=_say(session, thread, f"apply to {POSTING_URL}")
        )
        run_manager_turn(session, thread=thread, last=_say(session, thread, "yes"))
        # No JobMatch exists at all, so a bar check would have had nothing to
        # score and would have refused.
        assert session.scalars(select(Application)).one().job_match_id is None


class TestAParkedRequestIsVisibleToAPerson:
    """A request nothing surfaces is a candidate who asked us to apply and
    silently never heard back. The gap is not marked ``declined`` -- nobody
    refused, and recording a refusal that did not happen puts an invented
    answer into a record that matching reads."""

    def test_parking_flags_the_thread_for_review(
        self, session, account, channel, no_network
    ):
        thread = _managing_thread(session, account["org_id"])
        run_manager_turn(
            session, thread=thread, last=_say(session, thread, f"apply to {POSTING_URL}")
        )
        for reply in ("yes", "priya@example.com", "Priya Raman", "ok", "hello"):
            run_manager_turn(session, thread=thread, last=_say(session, thread, reply))

        assert applylink.pending(thread).status == applylink.STATUS_PARKED
        assert thread.review_needed is True
        assert applylink.PARKED_REASON in thread.review_reason

    def test_resolving_it_clears_only_our_reason(self, session, account):
        thread = _managing_thread(session, account["org_id"])
        thread.review_reason = "somebody else's reason"
        applylink.park(thread)
        assert "somebody else's reason" in thread.review_reason
        assert applylink.PARKED_REASON in thread.review_reason

        applylink.clear(thread)
        assert thread.review_reason == "somebody else's reason"

    def test_the_reason_never_contains_the_separator(self):
        """`identity._join_reasons` splits on "; ", so a reason holding one
        stops matching itself -- which is how attributed calls once stayed on
        the review queue forever."""
        assert "; " not in applylink.PARKED_REASON


class TestABoardIndexIsNotAPosting:
    """The confirm would catch these -- the candidate would say no -- but only
    after a row titled "Jobs" is in the database and matching against
    everybody."""

    @pytest.mark.parametrize(
        "title",
        ["Jobs at Acme", "Careers | Acme", "Open Roles - Acme", "We're hiring at Acme"],
    )
    def test_an_index_page_title_yields_nothing(self, title):
        page = f"<html><head><title>{title}</title></head><body>x</body></html>"
        assert read_posting(POSTING_URL, client=_Client(page)) is None

    def test_a_real_role_on_the_same_shape_still_reads(self):
        page = (
            "<html><head><title>Head of Finance at Acme</title></head>"
            "<body>x</body></html>"
        )
        read = read_posting(POSTING_URL, client=_Client(page))
        assert read is not None
        assert (read.title, read.company) == ("Head of Finance", "Acme")
