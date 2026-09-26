"""Voice intake: the coverage gate, transcript extraction, and caller lookup.

Three invariants everything here defends.

**The gate decides call length, not the model.** A cooperative caller who has
answered every required question must be releasable in two or three minutes, and
a caller who has answered four of eleven must not be. That decision is computed
from the record, so it is testable without making a phone call.

**A refusal is an answer.** If declining to discuss comp left the field
unanswered, the gate would never close and the agent would ask forever.

**Nothing invented.** Both extraction paths would rather leave a field empty
than fill it plausibly, because a preference the candidate never stated silently
rules out roles they would have taken and nobody ever sees what was filtered.
"""

from __future__ import annotations

import pytest
from intake_fixtures import (
    COMPLETE_PAYLOAD,
    COMPLETE_TRANSCRIPT,
    SPARSE_PAYLOAD,
    SPARSE_TRANSCRIPT,
)
from sqlalchemy import select

from juicebox.intake import (
    CORE_KEYS,
    ENRICHMENT_KEYS,
    EXTRACTOR_HEURISTIC,
    FIELDS_BY_KEY,
    REQUIRED_KEYS,
    SPEC_FIELDS,
    coverage,
    deferred_required,
    diff_records,
    empty_record,
    extract_intake,
    lookup_key,
    lookup_keys,
    next_questions,
    normalize_record,
)
from juicebox.intake.schema import is_filled
from juicebox.intake.service import progress_line
from juicebox.llm.client import FakeTransport, LLMClient, LLMUnavailable, set_llm
from juicebox.llm.prompts import _INTAKE_FIELD_KEYS
from juicebox.models import Candidate, CandidatePhone
from juicebox.repository import sync_candidate_phones


@pytest.fixture
def scripted_llm():
    """Install a FakeTransport-backed client and tear it down."""

    def install(*args, **kwargs):
        transport = FakeTransport(*args, **kwargs)
        set_llm(LLMClient(api_key="test-key", transport=transport, enabled=True))
        return transport

    yield install
    set_llm(None)


@pytest.fixture
def no_llm():
    set_llm(LLMClient(api_key="", transport=None, enabled=False))
    yield
    set_llm(None)


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


class TestCoverageGate:

    def test_answering_the_required_set_closes_the_gate(self):
        record = _filled_required()
        state = coverage(record)
        assert state.required_complete
        assert state.required_missing == ()
        # Enrichment is still outstanding: the gate is permission to wrap up,
        # not a claim that the record is finished.
        assert set(state.enrichment_missing) == set(ENRICHMENT_KEYS)

    def test_a_refusal_closes_the_gate_for_that_field(self):
        """The reason a caller who won't discuss comp can still end the call."""
        record = _filled_required()
        record["target_base"] = None
        record["target_total"] = None
        assert "compensation" in coverage(record).required_missing

        record["declined"] = ["compensation"]
        state = coverage(record)
        assert state.required_complete
        assert state.declined == ("compensation",)
        assert "compensation" in state.covered

    def test_a_declared_no_is_an_answer(self):
        """remote_ok=False means 'wants an office', which is not 'never asked'."""
        assert is_filled(False) is True
        record = empty_record()
        record["spec"]["remote_ok"] = False
        assert "spec.remote_ok" in coverage(record).covered

    def test_an_empty_list_is_not_an_answer(self):
        record = empty_record()
        record["active_processes"] = []
        assert "active_processes" in coverage(record).required_missing
        # Saying "nowhere" is an answer and has to be recorded as one.
        record["active_processes"] = [{"company": "none", "stage": "none"}]
        assert "active_processes" in coverage(record).covered

    def test_next_questions_moves_to_enrichment_once_the_gate_closes(self):
        asked = next_questions(_filled_required(), limit=2)
        assert [q["field"] for q in asked] == list(ENRICHMENT_KEYS[:2])

    def test_a_finished_record_asks_nothing(self):
        record = _filled_required()
        record["declined"] = list(ENRICHMENT_KEYS)
        assert next_questions(record) == []

    def test_core_only_closes_without_must_have_skills(self):
        """The heuristic cannot fill must-haves; requiring them on a short call
        would run every model-less pass to the 15-minute ceiling."""
        record = _filled_core()
        assert "spec.must_have_skills" not in coverage(record, core_only=True).covered
        assert coverage(record, core_only=True).required_complete
        assert not coverage(record).required_complete
        assert "spec.must_have_skills" in deferred_required(record)

    def test_seniority_does_not_block_a_short_call(self):
        """It is derivable from the titles, and asking for it out loud landed as
        jargon on a real call -- "what do you mean by level?" -- while blocking
        the gate, so a candidate could not finish until they had answered a
        question they did not understand. Still required on a full pass; never
        core."""
        assert "spec.seniority" not in CORE_KEYS
        assert FIELDS_BY_KEY["spec.seniority"].core is False
        assert "spec.seniority" in REQUIRED_KEYS


class TestProgressLine:

    def test_states_the_true_count_on_milestone(self):
        assert progress_line(5) == "Five more quick ones and I'll let you go."
        assert progress_line(3) == "Three more quick ones and I'll let you go."
        assert progress_line(1) == "One more quick one and I'll let you go."
        assert "4" not in progress_line(5)
        assert "2" not in progress_line(3)


class TestFieldDefinitions:

    def test_the_prompt_enum_matches_the_field_set(self):
        """prompts.py duplicates these keys to avoid an import cycle; this is
        the guard that keeps the copy honest."""
        assert set(_INTAKE_FIELD_KEYS) == set(FIELDS_BY_KEY)

    def test_the_spec_only_claims_candidate_stateable_fields(self):
        from juicebox.search.parser import ParsedQuery

        known = set(ParsedQuery.__dataclass_fields__)
        assert set(SPEC_FIELDS) <= known
        # A candidate has no opinion about which school they attended.
        assert "schools" not in SPEC_FIELDS
        assert "search_text" not in SPEC_FIELDS


# --------------------------------------------------------------------------
# extraction, model path
# --------------------------------------------------------------------------


class TestModelExtraction:

    def test_free_text_is_normalised_into_the_job_side_vocabulary(self, scripted_llm):
        """The whole premise: a preference has to land on the same tokens a
        parsed job description lands on, or it can never be scored against one."""
        scripted_llm(default=COMPLETE_PAYLOAD)
        spec = extract_intake(COMPLETE_TRANSCRIPT).record["spec"]

        assert spec["job_titles"] == ["machine learning engineer"]
        assert spec["locations"] == ["san francisco"]
        assert spec["industries"] == ["fintech"]
        assert "kubernetes" in spec["nice_to_have_skills"]
        assert spec["remote_ok"] is False

    def test_a_sparse_call_keeps_the_gate_open(self, scripted_llm):
        scripted_llm(default=SPARSE_PAYLOAD)
        result = extract_intake(SPARSE_TRANSCRIPT)

        assert not result.coverage.required_complete
        assert result.review_needed
        # Refused comp is resolved; the rest is genuinely still owed.
        assert "compensation" not in result.coverage.required_missing
        assert "timeline" in result.coverage.required_missing

    def test_missing_share_permission_forces_review(self, scripted_llm):
        scripted_llm(default=SPARSE_PAYLOAD)
        result = extract_intake(SPARSE_TRANSCRIPT)
        assert "permission" in result.review_reason

    def test_a_claimed_sharing_permission_does_not_clear_review(self, scripted_llm):
        """The one field authorising us to put a person in front of employers is
        also the one a model will invent. Asked about a transcript where sharing
        never came up, GLM 5.2 asserted true 3/5 and gpt-oss-120b 2/5. So a
        claim is evidence, not permission -- it still closes the coverage gate,
        because the call did its job, but a human confirms before it counts."""
        scripted_llm(default=COMPLETE_PAYLOAD)
        result = extract_intake(COMPLETE_TRANSCRIPT)
        assert result.record["ok_to_share_with_clients"] is True
        assert result.coverage.required_complete
        assert result.review_needed
        assert "not yet confirmed" in result.review_reason

    def test_the_timeline_bucket_comes_from_the_quote_not_the_model(self, scripted_llm):
        """Sorting a duration into five buckets is arithmetic. Left to the
        model it was non-deterministic on a required field: five runs on "two
        weeks if the right thing came up" gave GLM 1_3_months three times,
        3_6_months once and immediate once."""
        payload = _clone(COMPLETE_PAYLOAD)
        payload["timeline"] = "3_6_months"          # what the model chose
        payload["timeline_verbatim"] = "two weeks"  # what the candidate said
        scripted_llm(default=payload)
        record = extract_intake(COMPLETE_TRANSCRIPT).record
        assert record["timeline"] == "under_1_month"
        assert record["timeline_verbatim"] == "two weeks"

    def test_a_figure_without_a_currency_is_marked_not_assumed(self, scripted_llm):
        payload = _clone(COMPLETE_PAYLOAD)
        payload["comp_currency"] = ""
        scripted_llm(default=payload)
        record = extract_intake(COMPLETE_TRANSCRIPT).record
        assert record["target_base"] == 240000
        assert record["comp_currency"] == "UNKNOWN"

    def test_a_model_failure_degrades_instead_of_raising(self, scripted_llm):
        """Still degrades rather than raising -- but says which kind of
        heuristic read this is. `EXTRACTOR_HEURISTIC` means nobody configured a
        model; this means one was configured and did not answer."""
        from juicebox.intake.extract import EXTRACTOR_HEURISTIC_DEGRADED

        scripted_llm([LLMUnavailable("model down")])
        result = extract_intake(COMPLETE_TRANSCRIPT)
        assert result.extractor == EXTRACTOR_HEURISTIC_DEGRADED
        assert result.extractor != EXTRACTOR_HEURISTIC
        assert result.review_needed


class TestTimelineBucketing:
    """One definition of what "two weeks" means, shared by both paths, so the
    model path and the no-model path cannot disagree about a required field."""

    @pytest.mark.parametrize(
        ("said", "bucket"),
        [
            ("two weeks", "under_1_month"),
            ("within a month", "under_1_month"),
            ("next quarter", "1_3_months"),
            ("right away", "immediate"),
            ("six months", "3_6_months"),
            ("no rush", "exploratory"),
            ("", ""),
            ("someday", ""),
        ],
    )
    def test_phrases_map_deterministically(self, said, bucket):
        from juicebox.intake.extract import _bucket_timeline

        assert _bucket_timeline(said) == bucket


class TestFromARealCall:
    """Regressions from the first live call, 2026-08-22, 223 seconds.

    Every one is a cheap model filling a field it had no evidence for, or a
    taxonomy gap that made a real answer unmatchable. The transcript is the
    specification here: these are not hypotheticals.
    """

    def test_a_placeholder_does_not_close_a_required_field(self):
        """The agent never asked about work authorisation. The model wrote
        "unknown", which `is_filled` counts as present, so the gate closed on an
        unasked question -- and a client-blocking one."""
        record = normalize_record({"work_authorization": "unknown"})
        assert record["work_authorization"] == ""
        assert "work_authorization" in coverage(record).required_missing

    @pytest.mark.parametrize(
        "placeholder", ["unknown", "N/A", "not specified", "Not discussed", "-", "null"]
    )
    def test_placeholder_spellings_are_all_rejected(self, placeholder):
        assert normalize_record({"work_authorization": placeholder})["work_authorization"] == ""

    def test_a_real_answer_is_not_mistaken_for_a_placeholder(self):
        assert normalize_record({"work_authorization": "us citizen"})[
            "work_authorization"
        ] == "us citizen"

    def test_consent_without_a_quote_is_discarded(self):
        """Sharing permission was never discussed and the model returned true
        anyway. Quote or it did not happen."""
        record = normalize_record({"ok_to_share_with_clients": True})
        assert record["ok_to_share_with_clients"] is None
        assert "ok_to_share_with_clients" in coverage(record).required_missing

    def test_consent_with_a_quote_is_kept(self):
        record = normalize_record(
            {"ok_to_share_with_clients": True, "ok_to_share_verbatim": "Yes, go ahead."}
        )
        assert record["ok_to_share_with_clients"] is True

    def test_an_explicit_refusal_needs_no_quote(self):
        """False is never the convenient answer, so nothing is gained by
        fabricating it -- and demanding a quote would lose a real refusal."""
        assert normalize_record({"ok_to_share_with_clients": False})[
            "ok_to_share_with_clients"
        ] is False

    def test_a_conditional_timeline_is_exploratory(self):
        """Verbatim from the call. The model answered under_1_month, which puts a
        browsing candidate in front of a client hiring now."""
        record = normalize_record(
            {
                "timeline": "under_1_month",
                "timeline_verbatim": (
                    "If I find the right role, I might wanna start soon. "
                    "But if not, I'm okay to stay."
                ),
            }
        )
        assert record["timeline"] == "exploratory"

    def test_member_of_technical_staff_is_matchable(self):
        """Noise-stripping reduced it to "member technical", which matches no
        posting -- losing the whole point of sharing a vocabulary with the JD
        parser, for a title a real candidate actually said."""
        from juicebox.enrich import taxonomy as tx

        assert tx.normalize_title("Member of technical staff") == "software engineer"
        assert tx.normalize_title("MTS") == "software engineer"

    def test_a_model_chosen_timeline_bucket_is_flagged(self, scripted_llm):
        """When the quote maps to nothing, the model's bucket is used and nothing
        deterministic vouches for it. Say so."""
        payload = _clone(COMPLETE_PAYLOAD)
        payload["timeline"] = "1_3_months"
        payload["timeline_verbatim"] = "sometime after the reorg settles"
        scripted_llm(default=payload)
        result = extract_intake(COMPLETE_TRANSCRIPT)
        assert result.record["timeline"] == "1_3_months"
        assert "timeline" in result.low_confidence


class TestDefensiveCoercion:
    """Schema-constrained calls still arrive through here from replays, scripted
    fakes and future schema revisions. A wrong type must not reach the DB."""

    def test_garbage_becomes_an_empty_record_not_an_exception(self):
        record = normalize_record({"spec": "not a dict", "target_base": "lots"})
        assert record["spec"]["job_titles"] == []
        assert record["target_base"] is None
        assert coverage(record).required_missing

    def test_an_out_of_range_enum_is_dropped(self):
        record = normalize_record({"timeline": "whenever", "employment_status": "vibing"})
        assert record["timeline"] == ""
        assert record["employment_status"] == ""


# --------------------------------------------------------------------------
# extraction, heuristic path
# --------------------------------------------------------------------------


class TestHeuristicExtraction:

    def test_it_reads_the_unambiguous_fields(self, no_llm):
        record = extract_intake(COMPLETE_TRANSCRIPT).record
        assert record["timeline"] == "under_1_month"
        assert record["employment_status"] == "employed_searching"
        assert record["work_authorization"] == "green card"
        assert record["spec"]["remote_ok"] is False

    def test_it_reads_comp_from_a_sentence_about_comp(self, no_llm):
        record = extract_intake(COMPLETE_TRANSCRIPT).record
        assert record["target_base"] == 240000
        assert record["comp_currency"] == "UNKNOWN"

    def test_it_ignores_money_that_is_not_pay(self, no_llm):
        transcript = "Candidate: We raised $200 million last year and the team tripled."
        assert extract_intake(transcript).record["target_base"] is None

    def test_it_never_invents_a_must_have(self, no_llm):
        """A regex cannot tell 'I want to keep writing Rust' from 'I did some
        Rust once', and must_have_skills is what scoring penalises hard."""
        spec = extract_intake(COMPLETE_TRANSCRIPT).record["spec"]
        assert spec["must_have_skills"] == []
        assert "pytorch" in spec["nice_to_have_skills"]

    def test_it_never_infers_permission_to_share(self, no_llm):
        """The one field where a wrong guess is a permission we did not have."""
        record = extract_intake(COMPLETE_TRANSCRIPT).record
        assert record["ok_to_share_with_clients"] is None

    def test_it_never_invents_a_verbatim_quote(self, no_llm):
        assert extract_intake(COMPLETE_TRANSCRIPT).record["highlights"] == []

    def test_it_always_asks_for_review(self, no_llm):
        result = extract_intake(COMPLETE_TRANSCRIPT)
        assert result.review_needed
        assert "heuristic" in result.review_reason

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("I want something fully remote.", True),
            ("Fully remote isn't what I want, I like being around people.", None),
            ("I'd rather not be fully remote.", None),
            ("I want to be in the office most days.", False),
        ],
    )
    def test_a_negated_preference_is_not_read_as_the_preference(
        self, no_llm, said, expected
    ):
        """"fully remote isn't what I want" contains "fully remote" and means the
        opposite. Getting this backwards mis-files the preference that rules out
        the most roles."""
        record = extract_intake(f"Candidate: {said}").record
        assert record["spec"]["remote_ok"] is expected

    def test_a_negation_does_not_cross_a_clause_boundary(self, no_llm):
        """From a real call. "Hybrid is fine, fully remote isn't what I want"
        asserts one thing and denies another eleven characters apart, so a
        character-radius negation window swallows the assertion and records
        nothing. Negation is scoped to its clause."""
        record = extract_intake(
            "Candidate: Austin. Hybrid is fine, fully remote isn't what I want."
        ).record
        assert record["spec"]["remote_ok"] is False

    def test_an_explicit_level_beats_the_one_inferred_from_the_title(self, no_llm):
        """Also from a real call. infer_seniority_from_title answers "mid" for
        any unmarked title, so merging produced ["mid", "staff"] — a level the
        candidate never said, on a field clients filter hard against."""
        record = extract_intake(
            "Candidate: I'm after a staff backend engineer role, staying hands-on in Go."
        ).record
        assert record["spec"]["seniority"] == ["staff"]

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("I want to become a machine learning engineer.", "machine learning engineer"),
            ("I am hoping to move into a product manager position.", "product manager"),
            ("I want to be a recruiter.", "recruiter"),
            ("I want to become a staff data scientist.", "data scientist"),
            ("I'm trying to move into a front-end developer job.", "front-end developer"),
        ],
    )
    def test_a_title_stated_without_the_word_role_is_captured_whole(
        self, no_llm, said, expected
    ):
        """"I want to become a <title>" has no word after the title to stop on,
        so the capture ended on a word boundary and a lazy quantifier took the
        first one going: the end of the first word. "machine learning engineer"
        arrived as "machine", failed the role-noun check and was discarded. Only
        single-word titles survived, and "I want to be a recruiter" passing is
        what hid it."""
        record = extract_intake(f"Candidate: {said}").record
        assert record["spec"]["job_titles"] == [expected]

    def test_the_capture_stops_at_the_title_and_not_at_the_sentence(self, no_llm):
        """Ending the capture at the sentence rather than the head noun would
        trade one silent failure for another: "machine learning engineer at a
        startup" is a title no posting carries, on the field
        `search.scoring.assess` penalises hardest."""
        record = extract_intake(
            "Candidate: I want to become a machine learning engineer at a startup"
        ).record
        assert record["spec"]["job_titles"] == ["machine learning engineer"]

    def test_talking_about_both_records_neither(self, no_llm):
        result = extract_intake(
            "Candidate: I'd do fully remote or hybrid, whichever. Both work for me."
        )
        assert result.record["spec"]["remote_ok"] is None
        assert "spec.remote_ok" in result.low_confidence

    def test_a_negated_relocation_is_not_read_as_willing(self, no_llm):
        record = extract_intake("Candidate: I'm not willing to relocate, sorry.").record
        assert record["willing_to_relocate"] is False

    def test_an_explicit_none_on_processes_is_recorded(self, no_llm):
        record = extract_intake("Candidate: I'm not interviewing anywhere else.").record
        assert record["active_processes"] == [
            {"company": "none", "stage": "none", "expected_decision": ""}
        ]


class TestDisagreement:

    def test_a_changed_field_is_reported_with_both_readings(self):
        # Deliberately not `timeline`: that one is derived from the quote, so two
        # extractions of the same call agree no matter what bucket each chose.
        a = normalize_record(COMPLETE_PAYLOAD)
        b = normalize_record({**COMPLETE_PAYLOAD, "employment_status": "employed_open"})
        diff = diff_records(a, b)
        assert diff["employment_status"] == {
            "a": "employed_searching",
            "b": "employed_open",
        }


# --------------------------------------------------------------------------
# caller lookup
# --------------------------------------------------------------------------


class TestPhoneLookupKey:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("+14155550123", "+14155550123"),
            ("+1 (415) 555-0123", "+14155550123"),
            ("(415) 555-0123", "+14155550123"),
            ("415-555-0123", "+14155550123"),
            ("14155550123", "+14155550123"),
            ("+44 20 7946 0958", "+442079460958"),
        ],
    )
    def test_known_shapes_normalise(self, raw, expected):
        assert lookup_key(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", None, "555-0123", "987654321", "extension 4102", "n/a"],
    )
    def test_an_unknowable_country_gets_no_key(self, raw):
        """Refusing to guess is the point: a wrong key attaches a caller to
        somebody else's record, and the dial-in agent then reads it out."""
        assert lookup_key(raw) == ""

    def test_two_spellings_of_one_number_collapse(self):
        assert lookup_keys(["(415) 555-0123", "+1 415 555 0123"]) == ["+14155550123"]

    @pytest.mark.parametrize(
        "raw",
        [
            "+266696687",  # ANONYMOUS
            "+1266696687",  # ...as a North American carrier passes it on
            "+7378742833",  # RESTRICTED
            "+86282452253",  # UNAVAILABLE
            "+94844353",  # WITHHELD
            "0000000000",  # what a switch sends for "no caller ID"
            "+10000000000",
            "9999999999",
        ],
    )
    def test_a_caller_id_sentinel_gets_no_key(self, raw):
        """A withheld caller ID is not a number, however well-formed it looks.

        These are shaped like valid E.164 and used to pass, which made every
        anonymous caller share one key -- the exact wrong-person match this
        module exists to prevent.
        """
        assert lookup_key(raw) == ""

    def test_a_real_number_is_not_mistaken_for_a_sentinel(self):
        """The region-strip and repeated-digit rules must not eat real numbers."""
        assert lookup_key("+12666966870") == "+12666966870"
        assert lookup_key("+14155550123") == "+14155550123"
        assert lookup_key("(415) 555-0123") == "+14155550123"


class TestPhoneProjection:
    def _candidate(self, session, org_id, name, phones, source_id):
        candidate = Candidate(
            org_id=org_id,
            source="test",
            source_id=source_id,
            full_name=name,
            phones=phones,
        )
        session.add(candidate)
        session.flush()
        sync_candidate_phones(session, candidate)
        return candidate

    def test_an_unnormalisable_number_gets_no_row(self, session, account):
        self._candidate(session, account["org_id"], "Grace", ["987654321"], "grace")
        assert session.scalars(select(CandidatePhone)).all() == []

    def test_the_first_claim_on_a_number_wins(self, session, account):
        org_id = account["org_id"]
        first = self._candidate(session, org_id, "Ada", ["+14155550123"], "ada")
        self._candidate(session, org_id, "Someone Else", ["+14155550123"], "else")

        rows = session.scalars(select(CandidatePhone)).all()
        assert len(rows) == 1
        assert rows[0].candidate_id == first.id

    def test_removing_a_number_removes_its_row(self, session, account):
        candidate = self._candidate(
            session, account["org_id"], "Ada", ["+14155550123", "+15125550199"], "ada"
        )
        candidate.phones = ["+15125550199"]
        sync_candidate_phones(session, candidate)

        rows = session.scalars(select(CandidatePhone)).all()
        assert [(r.phone, r.position) for r in rows] == [("+15125550199", 0)]

    def test_duplicate_spellings_do_not_raise(self, session, account):
        """Same number twice in one JSON list is one row, not an IntegrityError."""
        self._candidate(
            session,
            account["org_id"],
            "Ada",
            ["(415) 555-0123", "+1 415 555 0123"],
            "ada",
        )
        rows = session.scalars(select(CandidatePhone)).all()
        assert len(rows) == 1


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _filled_core() -> dict:
    """The five gate-closing fields, and nothing else. Skills deliberately empty."""
    record = empty_record()
    record["spec"].update(
        {
            "job_titles": ["machine learning engineer"],
            "seniority": ["senior"],
            "locations": ["san francisco"],
            "remote_ok": False,
        }
    )
    record["target_base"] = 240000.0
    record["comp_currency"] = "USD"
    return record


def _filled_required() -> dict:
    """A record with every required field answered and no enrichment."""
    record = empty_record()
    record["spec"].update(
        {
            "job_titles": ["machine learning engineer"],
            "seniority": ["senior"],
            "locations": ["san francisco"],
            "remote_ok": False,
            "must_have_skills": ["pytorch"],
        }
    )
    record.update(
        {
            "employment_status": "employed_searching",
            "timeline": "under_1_month",
            "target_base": 240000.0,
            "comp_currency": "USD",
            "work_authorization": "green card",
            "active_processes": [{"company": "none", "stage": "none"}],
            "ok_to_share_with_clients": True,
        }
    )
    return record


def _clone(payload: dict) -> dict:
    import copy

    return copy.deepcopy(payload)


class TestPersonaCopy:
    """``deploy/`` is stdlib-only and cannot import ``juicebox.config``, so the
    persona exists twice. The prompt and the dynamic variable have to agree, or
    the agent greets as one name and refers to itself by another mid-call.
    Same idiom as ``test_the_prompt_enum_matches_the_field_set``."""

    def _deploy_constants(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        text = (root / "deploy" / "retell_agents.py").read_text(encoding="utf-8")
        found = {}
        for name in ("RECRUITER_NAME", "SMS_NUMBER_SPOKEN"):
            match = re.search(rf'^{name} = "([^"]*)"', text, re.M)
            assert match, f"{name} not found in deploy/retell_agents.py"
            found[name] = match.group(1)
        return found

    def test_the_persona_matches_the_deploy_script(self):
        from juicebox.config import get_settings

        settings = get_settings()
        deploy = self._deploy_constants()
        assert deploy["RECRUITER_NAME"] == settings.intake_recruiter_name
        assert deploy["SMS_NUMBER_SPOKEN"] == settings.intake_sms_number_spoken
