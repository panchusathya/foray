"""Applying to a board nobody mapped: derived specs, the ask loop, warm state.

Three claims under test, each the inversion of a live failure:

* An unknown board with a usable derived spec IS carded, and one without is
  not -- with a derivation queued so the next match has the answer. Before
  this, ``can_submit_vendor("")`` made the unknown frontier invisible.
* Chat asks only missing work authorization. A required custom essay is not
  surveyed; the submitter hands off if it cannot fill it.
* The candidate's browser state is a credential: filtered per run's hosts,
  merged on save, and never handed to a host it was not made on.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from test_messaging_manager import _managed, _posting

from juicebox.applications import followups, formspec
from juicebox.applications.ats import detect
from juicebox.applications.service import create_application
from juicebox.applications.submit import registry
from juicebox.applications.submit.browser.derive import derive_spec
from juicebox.applications.submit.browser.forms import FormSpec
from juicebox.jobs import KIND_JOBS_FORMSPEC_ONE
from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.threads import open_thread
from juicebox.models import Application, AtsAccount, Job, JobMatch, JobPosting, utcnow

STRONG = {"name": "skills", "label": "Skills", "score": 0.9, "evidence": "strong"}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def channel():
    fake = FakeChannel(consent=True)
    set_channel(LINQ, fake)
    return fake


@pytest.fixture
def thread(session, account, channel):
    return open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-derived",
        entry_point="linq_inbound",
        from_address="+15134333227",
        consent_messaging=True,
    )


@pytest.fixture
def live_mode(monkeypatch):
    from juicebox.config import reset_settings_cache

    monkeypatch.setenv("APPLY_SUBMITTER", "live")
    reset_settings_cache()
    registry.reset_overrides()
    yield
    registry.reset_overrides()
    reset_settings_cache()


@pytest.fixture
def memory_mode(monkeypatch):
    from juicebox.config import reset_settings_cache

    monkeypatch.setenv("APPLY_SUBMITTER", "memory")
    reset_settings_cache()
    registry.reset_overrides()
    yield registry.memory_submitter()
    registry.reset_overrides()
    reset_settings_cache()


def _controls(**overrides) -> list[dict]:
    """A plausible one-page form, as _ENUMERATE_JS would report it."""
    base = [
        {"label": "Full Name", "selector": "input[name=\"name\"]", "fieldType": "text", "required": True},
        {"label": "Email", "selector": "input[name=\"email\"]", "fieldType": "text", "required": True},
        {"label": "Phone", "selector": "input[name=\"phone\"]", "fieldType": "text", "required": False},
        {"label": "Resume/CV", "selector": "#resume", "fieldType": "file", "required": True},
        {"label": "Why us?", "selector": "textarea[name=\"why\"]", "fieldType": "textarea", "required": False},
    ]
    if overrides.get("drop_resume"):
        base = [c for c in base if c["fieldType"] != "file"]
    if overrides.get("password"):
        base.append({"label": "Password", "selector": "#pw", "fieldType": "password", "required": True})
    return base


_BUTTONS = [
    {"text": "Apply now", "selector": 'a:has-text("Apply now")', "tag": "a", "type": ""},
    {"text": "Submit application", "selector": 'button:has-text("Submit application")', "tag": "button", "type": "submit"},
]


def _unknown_posting(session, org_id, *, suffix="u1", spec=None, unusable=False) -> JobPosting:
    """A posting on a host detect.py cannot name, resolution already cached."""
    posting = JobPosting(
        org_id=org_id,
        source="exa",
        source_id=f"derived-{suffix}",
        title="Platform Engineer",
        company="Nimbus Robotics",
        company_normalized="nimbus robotics",
        jd_text="Build things.",
        status="open",
        source_url=f"https://careers.nimbusrobotics.example/jobs/{suffix}",
        # Stamped-unrecognised: resolve_target answers from the row, no network.
        ats_vendor="",
        ats_resolved_at=utcnow(),
    )
    if spec is not None:
        posting.form_spec = spec.to_dict()
        posting.form_spec_checked_at = utcnow()
    if unusable:
        posting.form_spec = {formspec.UNUSABLE_KEY: "no derivable form"}
        posting.form_spec_checked_at = utcnow()
    session.add(posting)
    session.flush()
    return posting


def _spec() -> FormSpec:
    spec = derive_spec(_controls(), _BUTTONS)
    assert spec is not None
    return spec


# ---------------------------------------------------------------------------
# deriving the spec
# ---------------------------------------------------------------------------


class TestDeriveSpec:
    def test_a_plain_application_form_derives(self):
        spec = _spec()
        assert spec.fields["resume"] == ("#resume",)
        assert spec.files == frozenset({"resume"})
        assert {"email", "resume", "full_name"} <= set(spec.required)
        # type=submit outranks the Apply control that opened the form.
        assert spec.submit[0] == 'button:has-text("Submit application")'
        # The landmark is a field we know is there -- the Ashby lesson.
        assert spec.landmark[0] == 'input[name="email"]'

    def test_a_newsletter_is_not_an_application(self):
        """An email field alone is a mailing list; only a form that wants a
        CV is a form we may promise to fill."""
        assert derive_spec(_controls(drop_resume=True), _BUTTONS) is None

    def test_a_password_control_refuses_the_page(self):
        """A password means an account wall, and 'filling' one types a fresh
        password into a sign-in form -- the base.py disaster, generalised."""
        assert derive_spec(_controls(password=True), _BUTTONS) is None

    def test_the_autofill_trap_prefers_the_labelled_resume(self):
        controls = [
            {"label": "", "selector": "input[type=file]", "fieldType": "file", "required": False},
            {"label": "Resume", "selector": "#real-resume", "fieldType": "file", "required": True},
            {"label": "Email", "selector": "#email", "fieldType": "text", "required": True},
        ]
        spec = derive_spec(controls, _BUTTONS)
        assert spec is not None
        assert spec.fields["resume"] == ("#real-resume",)

    def test_an_eeoc_signature_box_is_never_an_identity_field(self):
        """Found live on JazzHR: the EEOC disability signature's label reads
        as a name field, and the first probe mapped full_name onto
        #resumator-eeoc_disability_signature-value -- auto-signing a legal
        self-identification. The label lies; the id does not."""
        controls = [
            *_controls(),
            {
                "label": "Name",
                "selector": "#resumator-eeoc_disability_signature-value",
                "fieldType": "text",
                "required": False,
            },
        ]
        spec = derive_spec(controls, _BUTTONS)
        assert spec is not None
        assert all("eeoc" not in sel for sels in spec.fields.values() for sel in sels)
        # The plain name field still maps -- the guard reads selectors, not labels.
        assert spec.fields["full_name"] == ('input[name="name"]',)

    def test_a_field_with_no_selector_is_dropped(self):
        """driver.fill cannot address what enumeration could not name, and a
        spec whose email dropped is no spec at all."""
        controls = [
            {"label": "Email", "selector": "", "fieldType": "text", "required": True},
            {"label": "Resume", "selector": "#r", "fieldType": "file", "required": True},
        ]
        assert derive_spec(controls, _BUTTONS) is None

    def test_no_submit_control_no_spec(self):
        assert derive_spec(_controls(), []) is None

    def test_round_trip_survives_the_json_column(self):
        spec = _spec()
        back = FormSpec.from_dict(spec.to_dict())
        assert back is not None
        assert back.fields == spec.fields
        assert back.required == spec.required
        assert back.submit == spec.submit
        assert back.files == spec.files

    def test_an_unusable_marker_is_not_a_spec(self):
        assert FormSpec.from_dict({formspec.UNUSABLE_KEY: "x"}) is None


class TestOpeners:
    """Findings from the first live probe session (DigitalHire, Mindpal)."""

    def test_marketing_copy_is_not_an_opener(self):
        """mindpal.co's "Check if your resume is ATS-ready before applying"
        matched the loose regex and was clicked live. Following a control
        means trusting it; only text that STARTS as an apply control
        qualifies."""
        from juicebox.applications.submit.browser.derive import _openers

        buttons = [
            {"text": "Check if your resume is ATS-ready before applying",
             "selector": "a:has-text('Check')", "href": "https://x/tips"},
            {"text": "Apply to Job", "selector": "a:has-text('Apply to Job')",
             "href": "https://x/apply"},
            {"text": "I'm interested", "selector": "button:has-text('I')", "href": ""},
        ]
        assert [b["text"] for b in _openers(buttons)] == [
            "Apply to Job",
            "I'm interested",
        ]


# ---------------------------------------------------------------------------
# routing: registry and submitter_for
# ---------------------------------------------------------------------------


class TestRouting:
    def test_unknown_vendor_without_the_agent_stays_a_handoff(self, live_mode):
        target = detect.ApplyTarget(vendor="", apply_url="https://x.example/jobs/1")
        assert registry.get_submitter(target) is None
        assert registry.get_submitter(target, derived_spec=_spec()) is None

    def test_account_gated_vendors_never_take_the_agent(self, live_mode):
        for vendor in registry.ACCOUNT_GATED_VENDORS:
            target = detect.ApplyTarget(vendor=vendor, apply_url="https://x.example")
            submitter = registry.get_submitter(target, derived_spec=_spec())
            from juicebox.applications.submit.browser.agent import BrowserAgentSubmitter

            assert not isinstance(submitter, BrowserAgentSubmitter), vendor

    def test_console_mode_does_not_change_the_vendor_answer(self):
        assert registry.mode() == "console"
        assert not registry.can_submit_vendor("")
        assert not registry.can_submit_vendor("taleo")
        assert not registry.can_submit_vendor("greenhouse")
        assert registry.can_submit_vendor("yc")


def _seed_candidate(session, org_id) -> str:
    import uuid

    from juicebox.models import Candidate

    row = Candidate(
        org_id=org_id,
        source="manual",
        source_id=f"derived-{uuid.uuid4().hex[:8]}",
        full_name="Ada Okonkwo",
        emails=["ada-derived@example.com"],
    )
    session.add(row)
    session.flush()
    return row.id


def _application_for(session, org_id, candidate_id, posting) -> Application:
    session.add(
        JobMatch(
            org_id=org_id,
            job_posting_id=posting.id,
            candidate_id=candidate_id,
            score=0.9,
            criteria=[STRONG],
            status="new",
        )
    )
    session.flush()
    application = create_application(
        session,
        org_id=org_id,
        candidate_id=candidate_id,
        job_posting_id=posting.id,
        source="candidate",
    )
    application.status = "prepared"
    application.approved_at = utcnow()
    session.flush()
    return application


# ---------------------------------------------------------------------------
# the carding gate
# ---------------------------------------------------------------------------


class TestCardingGate:
    def _pairs(self, session, posting):
        match = JobMatch(
            org_id=posting.org_id,
            job_posting_id=posting.id,
            candidate_id=_seed_candidate(session, posting.org_id),
            score=0.9,
            criteria=[STRONG],
            status="new",
        )
        session.add(match)
        session.flush()
        return [(match, posting)]

    def test_a_derived_posting_is_carded(self, session, account, monkeypatch):
        from juicebox.messaging import manager

        monkeypatch.setattr("juicebox.jobmatch.liveness.is_gone", lambda url: False)
        posting = _unknown_posting(session, account["org_id"], suffix="c1", spec=_spec())
        kept = manager._sendable_postings(session, self._pairs(session, posting), limit=3)
        assert [p.id for _, p in kept] == [posting.id]

    def test_an_unchecked_unknown_is_carded_and_derivation_is_not_queued(
        self, session, account, monkeypatch
    ):
        from juicebox.messaging import manager

        monkeypatch.setattr("juicebox.jobmatch.liveness.is_gone", lambda url: False)
        posting = _unknown_posting(session, account["org_id"], suffix="c2")
        kept = manager._sendable_postings(session, self._pairs(session, posting), limit=3)
        assert [p.id for _, p in kept] == [posting.id]
        assert (
            session.scalar(
                select(Job).where(
                    Job.org_id == account["org_id"], Job.kind == KIND_JOBS_FORMSPEC_ONE
                )
            )
            is None
        )

    def test_a_checked_unusable_posting_is_still_carded_and_queues_nothing(
        self, session, account, monkeypatch
    ):
        """The answer is already on the row, so no new page view — but the
        match is still shown: an un-automatable form ends as a ready-to-review
        package with the direct link, not a hidden role."""
        from juicebox.messaging import manager

        monkeypatch.setattr("juicebox.jobmatch.liveness.is_gone", lambda url: False)
        posting = _unknown_posting(session, account["org_id"], suffix="c3", unusable=True)
        kept = manager._sendable_postings(session, self._pairs(session, posting), limit=3)
        assert [p.id for _, p in kept] == [posting.id]
        assert (
            session.scalar(
                select(Job).where(
                    Job.org_id == account["org_id"], Job.kind == KIND_JOBS_FORMSPEC_ONE
                )
            )
            is None
        )

    def test_derivation_is_gone_for_every_vendor(self, session, account):
        for vendor in ("greenhouse", "workday", "taleo", ""):
            posting = _unknown_posting(session, account["org_id"], suffix=f"c4-{vendor or 'none'}")
            posting.ats_vendor = vendor
            session.flush()
            assert formspec.enqueue_derivation(session, posting) is None


# ---------------------------------------------------------------------------
# the derivation job
# ---------------------------------------------------------------------------


class TestFormspecJob:
    def test_leftover_jobs_skip(self, session, account):
        posting = _unknown_posting(session, account["org_id"], suffix="j1")
        job = Job(
            org_id=account["org_id"],
            kind=KIND_JOBS_FORMSPEC_ONE,
            payload={"posting_id": posting.id},
            idempotency_key=f"formspec:{posting.id}",
        )
        session.add(job)
        session.flush()
        result = formspec.handle_formspec_one(session, job, utcnow())
        assert "skipped" in result


# ---------------------------------------------------------------------------
# package-time preflight
# ---------------------------------------------------------------------------


class TestPackageTimePreflight:
    def _application(self, session, account, posting):
        candidate_id = _seed_candidate(session, account["org_id"])
        return _application_for(session, account["org_id"], candidate_id, posting)

    def test_agent_preflight_merges_questions(self, session, account, monkeypatch):
        from juicebox.applications.questions import augment_with_form_questions
        from juicebox.applications.submit.browser import cloud as browser_cloud
        from juicebox.applications.submit.browser.agent import preflight
        from juicebox.applications.submit.browser.questions import FormQuestion

        posting = _unknown_posting(session, account["org_id"], suffix="r1")
        application = self._application(session, account, posting)
        monkeypatch.setattr(browser_cloud, "agent_configured", lambda: True)
        question = FormQuestion(
            key="q_new01",
            label="What draws you to robotics?",
            kind="custom",
            field_type="textarea",
            selector="#q_new",
            required=True,
        )
        monkeypatch.setattr(preflight, "discover_questions", lambda ctx: [question])

        merged, outcome = augment_with_form_questions(
            session, application=application, posting=posting, answers=[]
        )
        assert outcome == "ran"
        added = next(item for item in merged if item.get("key") == "q_new01")
        assert added["kind"] == "custom"
        assert added["form_required"] is True

    def test_unconfigured_agent_degrades_to_the_standard_set(
        self, session, account, monkeypatch
    ):
        from juicebox.applications.questions import augment_with_form_questions
        from juicebox.applications.submit.browser import cloud as browser_cloud

        posting = _unknown_posting(session, account["org_id"], suffix="r2")
        application = self._application(session, account, posting)
        monkeypatch.setattr(browser_cloud, "agent_configured", lambda: False)
        answers = [{"key": "salary_expectation", "answer": "$200k"}]
        merged, outcome = augment_with_form_questions(
            session, application=application, posting=posting, answers=answers
        )
        assert merged == answers
        assert "browser agent is not configured" in outcome


# ---------------------------------------------------------------------------
# the ask loop: work authorization only
# ---------------------------------------------------------------------------


QUESTION = "What interests you about infrastructure?"


def _asking_application(session, account, thread, *, work_auth="US citizen") -> tuple[Application, JobPosting]:
    """Prepared, consented; a required custom essay is blank on purpose."""
    posting = _posting(
        session, account["org_id"], company="Nimbus", title="Platform Engineer", suffix="ask"
    )
    application = _application_for(session, account["org_id"], thread.candidate_id, posting)
    application.form_answers = [
        {"key": "salary_expectation", "answer": "$220k", "kind": "standard"},
        {"key": "notice_period", "answer": "2 weeks", "kind": "standard"},
        {"key": "work_authorization", "answer": work_auth, "kind": "standard"},
        {"key": "why_company", "answer": "drafted words", "kind": "standard", "edited": True},
        {
            "key": "q_infra01",
            "question": QUESTION,
            "answer": "",
            "kind": "custom",
            "field_type": "textarea",
            "selector": "textarea[name=\"why\"]",
            "form_required": True,
        },
        {
            "key": "q_vet01",
            "question": "Veteran status",
            "answer": "",
            "kind": "attestation",
            "field_type": "select",
            "selector": "#vet",
            "form_required": False,
        },
    ]
    session.flush()
    return application, posting


class TestAskLoop:
    def test_auto_submit_does_not_withhold_on_a_custom_essay(
        self, session, account, channel, thread, memory_mode
    ):
        """A required custom blank used to park the send for a chat survey.
        The submitter hands off if it cannot fill it; chat does not ask."""
        from juicebox.applications.submission import auto_submit

        thread = _managed(session, thread)
        application, _ = _asking_application(session, account, thread)

        job, reason = auto_submit(session, application)
        assert job is not None, reason
        assert reason == "queued"

    def test_chat_does_not_ask_the_custom_essay(
        self, session, account, channel, thread, memory_mode
    ):
        thread = _managed(session, thread)
        application, _ = _asking_application(session, account, thread)

        result = followups.ask_for_missing(session, application)
        assert result.get("skipped") == "package answered everything"
        assert result.get("sent", 0) == 0
        assert channel.sent == []

    def test_missing_work_auth_is_asked_once(
        self, session, account, channel, thread, memory_mode
    ):
        thread = _managed(session, thread)
        application, _ = _asking_application(session, account, thread, work_auth="")

        result = followups.ask_for_missing(session, application)
        assert result["asked"] == ["work_authorization"]
        assert result["sent"] == 1
        body = channel.sent[-1][1].body.lower()
        assert "authoris" in body or "sponsor" in body
        assert "infrastructure" not in body
        assert "veteran" not in body

        sent_before = len(channel.sent)
        again = followups.ask_for_missing(session, application)
        assert again.get("skipped") == "already asked"
        assert len(channel.sent) == sent_before


# ---------------------------------------------------------------------------
# warm profiles
# ---------------------------------------------------------------------------


class TestProfiles:
    def test_filtering_releases_nothing_to_another_host(self):
        from juicebox.applications.submit.browser.profiles import filtered_state

        state = {
            "cookies": [
                {"name": "sess", "domain": ".host-a.example", "path": "/", "value": "s1"},
                {"name": "sess", "domain": "accounts.google.com", "path": "/", "value": "g"},
            ],
            "origins": [
                {"origin": "https://host-a.example", "localStorage": []},
                {"origin": "https://host-b.example", "localStorage": []},
            ],
        }
        a = filtered_state(state, hosts={"jobs.host-a.example"})
        assert a is not None
        assert [c["domain"] for c in a["cookies"]] == [".host-a.example"]
        assert [o["origin"] for o in a["origins"]] == ["https://host-a.example"]
        assert filtered_state(state, hosts={"host-c.example"}) is None

    def test_state_round_trips_encrypted_and_merges(self, session, account):
        from juicebox.applications.submit.browser.profiles import persist, profile_for
        from juicebox.models import BrowserProfile

        candidate_id = _seed_candidate(session, account["org_id"])
        profile = profile_for(
            session,
            org_id=account["org_id"],
            candidate_id=candidate_id,
            run_hosts={"host-a.example"},
        )
        assert profile is not None
        assert profile.storage_state is None
        token = profile.egress_session
        assert token, "sticky egress is minted with the row"

        profile.saved_state = {
            "cookies": [{"name": "sess", "domain": ".host-a.example", "path": "/", "value": "s1"}],
            "origins": [],
        }
        persist(session, profile, user_agent="UA-1")

        row = session.scalar(select(BrowserProfile))
        assert row.storage_state and "host-a" not in row.storage_state, "encrypted at rest"
        assert row.user_agent == "UA-1"

        # A later run against host B sees none of host A's jar...
        again_b = profile_for(
            session,
            org_id=account["org_id"],
            candidate_id=candidate_id,
            run_hosts={"host-b.example"},
        )
        assert again_b.storage_state is None
        assert again_b.egress_session == token, "one candidate, one egress IP"
        # ...and a run against host A gets it back.
        again_a = profile_for(
            session,
            org_id=account["org_id"],
            candidate_id=candidate_id,
            run_hosts={"host-a.example"},
        )
        assert again_a.storage_state is not None
        assert again_a.storage_state["cookies"][0]["value"] == "s1"

        # A save from host B's run merges over the jar, never replaces it.
        again_b.saved_state = {
            "cookies": [{"name": "other", "domain": ".host-b.example", "path": "/", "value": "b"}],
            "origins": [],
        }
        persist(session, again_b)
        both = profile_for(
            session,
            org_id=account["org_id"],
            candidate_id=candidate_id,
            run_hosts={"host-a.example", "host-b.example"},
        )
        assert {c["domain"] for c in both.storage_state["cookies"]} == {
            ".host-a.example",
            ".host-b.example",
        }

    def test_the_flag_and_cdp_gate_the_whole_feature(self, session, account, monkeypatch):
        from types import SimpleNamespace

        import juicebox.applications.submission as submission_mod
        from juicebox.applications.submission import _browser_profile_for

        posting = _unknown_posting(session, account["org_id"], suffix="p1", spec=_spec())
        candidate_id = _seed_candidate(session, account["org_id"])
        application = _application_for(session, account["org_id"], candidate_id, posting)
        submitter = SimpleNamespace(method="browser")
        target = detect.ApplyTarget(vendor="", apply_url="https://careers.x.example/1")

        def settings(profiles: bool, cdp: str = ""):
            return SimpleNamespace(
                apply_browser_profiles=profiles, apply_browser_cdp_url=cdp
            )

        # Off by default: byte-for-byte the proven cold context.
        monkeypatch.setattr(submission_mod, "get_settings", lambda: settings(False))
        assert _browser_profile_for(session, application, submitter, target) is None
        # CDP wins even with the flag on: the Scraping Browser owns its
        # fingerprint.
        monkeypatch.setattr(
            submission_mod, "get_settings", lambda: settings(True, "wss://cdp.example")
        )
        assert _browser_profile_for(session, application, submitter, target) is None
        # On, local Chromium, browser method: a profile.
        monkeypatch.setattr(submission_mod, "get_settings", lambda: settings(True))
        profile = _browser_profile_for(session, application, submitter, target)
        assert profile is not None
        # A non-browser submitter opens no page and gets no credential.
        api = SimpleNamespace(method="ats_api")
        assert _browser_profile_for(session, application, api, target) is None


# ---------------------------------------------------------------------------
# account handover
# ---------------------------------------------------------------------------


class TestHandover:
    def _account_row(self, session, account, candidate_id) -> AtsAccount:
        from juicebox.applications.submit.browser.workday import remember_account

        return remember_account(
            session,
            org_id=account["org_id"],
            candidate_id=candidate_id,
            vendor="workday",
            tenant="nimbus",
            username="anna+abc@goforay.io",
            password="generated-secret",
        )

    def test_a_handed_over_account_is_never_offered_again(self, session, account):
        from juicebox.applications.submit.browser.workday import stored_account

        candidate_id = _seed_candidate(session, account["org_id"])
        row = self._account_row(session, account, candidate_id)
        username, password = stored_account(
            session, candidate_id=candidate_id, vendor="workday", tenant="nimbus"
        )
        assert username and password == "generated-secret"

        row.handed_over_at = utcnow()
        session.flush()
        assert stored_account(
            session, candidate_id=candidate_id, vendor="workday", tenant="nimbus"
        ) == ("", "")

    def test_the_portal_never_returns_the_password(self, session, account, client):
        from juicebox.portal.links import mint_session_token

        candidate_id = _seed_candidate(session, account["org_id"])
        row = self._account_row(session, account, candidate_id)
        session.commit()
        headers = {
            "Authorization": f"Bearer {mint_session_token(org_id=account['org_id'], candidate_id=candidate_id)}"
        }

        listing = client.get("/v1/portal/accounts", headers=headers)
        assert listing.status_code == 200
        assert "generated-secret" not in listing.text
        entry = listing.json()[0]
        assert entry["vendor"] == "workday"
        assert entry["username"] == "anna+abc@goforay.io"
        assert entry["handed_over"] is False

        retired = client.post(f"/v1/portal/accounts/{row.id}/handover", headers=headers)
        assert retired.status_code == 410
        assert "generated-secret" not in retired.text
