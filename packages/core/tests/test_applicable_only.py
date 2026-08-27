"""The agent only promises what it can do, and only asserts what it was told.

Born from the first live run on the fresh number (2026-08-26): three cards on
boards with no submit adapter, a portal questionnaire presented as the
employer's form, and a work-authorization box holding a legal claim the model
inferred from a teaching-assistant job. Each class here pins one of the fixes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from juicebox.applications.ats import detect
from juicebox.applications.submit import registry
from juicebox.docgen import answers as answers_mod
from juicebox.llm.client import LLMResult
from juicebox.models import JobPosting


class _FakeLLM:
    """The docgen seam: .available and .json(...) -> LLMResult."""

    def __init__(self, payload: dict | None, *, available: bool = True):
        self.available = available
        self._payload = payload
        self.prompts: list[str] = []

    def json(self, *, system: str, prompt: str, schema: dict) -> LLMResult:
        self.prompts.append(prompt)
        return LLMResult(text="", data=self._payload, model="fake", stop_reason="end")


def _posting(org_id: str = "", **extra) -> JobPosting:
    base = dict(
        org_id=org_id,
        source="exa",
        source_id="applicable-1",
        title="Junior Software Developer",
        company="Wipro Limited",
        jd_text="Build things.",
        status="open",
    )
    base.update(extra)
    return JobPosting(**base)


class TestCanSubmitVendor:
    """The gate must not change its answer when APPLY_SUBMITTER changes."""

    @pytest.mark.parametrize("mode", ["console", "live", "memory"])
    def test_the_answer_is_mode_independent(self, mode, monkeypatch):
        monkeypatch.setattr(
            registry, "get_settings", lambda: SimpleNamespace(apply_submitter=mode)
        )
        assert registry.can_submit_vendor("yc")
        assert registry.can_submit_vendor("email")
        assert not registry.can_submit_vendor("greenhouse")
        assert not registry.can_submit_vendor("smartrecruiters")
        assert not registry.can_submit_vendor("workable")
        # Recognised is not submittable without the agent: we can name Taleo, not drive it.
        assert not registry.can_submit_vendor("taleo")
        assert not registry.can_submit_vendor("")

    def test_submittable_says_yes_to_everything_under_console(self):
        """The trap can_submit_vendor exists to avoid: the console double
        accepts any vendor, so `submittable` cannot gate a promise."""
        target = detect.ApplyTarget(vendor="taleo", apply_url="https://x", tier=3)
        assert registry.mode() == "console"
        assert registry.submittable(target)
        assert not registry.can_submit_vendor("taleo")


class TestResolveDoesNotPoisonOnMiss:
    def test_a_networked_miss_leaves_the_row_unstamped(self, session, account, monkeypatch):
        posting = _posting(account["org_id"], source_url="https://jobs.digitalhire.example/opening/x")
        session.add(posting)
        session.flush()
        # The follow hop finds nothing (an aggregator 403ing our IP).
        monkeypatch.setattr(detect, "follow", lambda url, client=None: detect.ApplyTarget())

        target = detect.resolve_target(session, posting, stamp_on_miss=False)

        assert not target
        assert posting.ats_resolved_at is None, "a miss must stay retryable"
        assert posting.ats_vendor == ""

    def test_a_hit_still_stamps(self, session, account):
        posting = _posting(
            account["org_id"],
            source_url="https://www.ycombinator.com/companies/deep24/jobs/xyz123-founding"
        )
        session.add(posting)
        session.flush()

        target = detect.resolve_target(session, posting, stamp_on_miss=False)

        assert target.vendor == "yc"
        assert posting.ats_resolved_at is not None
        assert posting.ats_tenant == "deep24"

    def test_a_yc_slug_fills_a_blank_company(self, session, account):
        posting = _posting(
            account["org_id"],
            company="",
            company_normalized="",
            source_url="https://www.ycombinator.com/companies/10x-science/jobs/FK8wuOk-eng",
        )
        session.add(posting)
        session.flush()

        detect.resolve_target(session, posting, stamp_on_miss=False)

        assert posting.company == "10X Science"

    def test_a_named_company_is_never_overwritten(self, session, account):
        posting = _posting(
            account["org_id"],
            company="Deep24 Inc",
            source_url="https://www.ycombinator.com/companies/deep24/jobs/xyz123-founding",
        )
        session.add(posting)
        session.flush()
        detect.resolve_target(session, posting, stamp_on_miss=False)
        assert posting.company == "Deep24 Inc"


class TestWorkAuthorizationIsNeverGenerated:
    """The fabricated-claim fix: the live package answered the sponsorship
    question from a TA job and a congressional internship, cited both, and the
    provenance!=none guard waved it through."""

    def _preference(self, **extra):
        base = dict(
            work_authorization="",
            target_base=None,
            target_total=None,
            comp_currency="",
            timeline="",
            willing_to_relocate=None,
            highlights=[],
        )
        base.update(extra)
        return SimpleNamespace(**base)

    def test_a_cited_resume_inference_is_discarded(self, monkeypatch):
        fabricated = {
            "answers": [
                {
                    "key": "work_authorization",
                    "answer": "I am authorized and will not need sponsorship.",
                    "provenance": "resume_json.experience[4].company (US Congress)",
                }
            ]
        }
        monkeypatch.setattr(answers_mod, "get_llm", lambda: _FakeLLM(fabricated))

        answers, _ = answers_mod.build_form_answers(
            preference=self._preference(),
            resume_json={"contact": {}},
            posting=_posting(),
        )

        work_auth = next(a for a in answers if a["key"] == "work_authorization")
        assert work_auth["answer"] == ""
        assert work_auth["provenance"] == "none"
        assert work_auth["generated_by"] == "template"

    def test_the_question_is_not_even_offered_to_the_model(self, monkeypatch):
        llm = _FakeLLM({"answers": []})
        monkeypatch.setattr(answers_mod, "get_llm", lambda: llm)
        answers_mod.build_form_answers(
            preference=self._preference(), resume_json={}, posting=_posting()
        )
        assert "work_authorization" not in llm.prompts[0]

    def test_a_stated_preference_ships_verbatim(self, monkeypatch):
        monkeypatch.setattr(answers_mod, "get_llm", lambda: _FakeLLM({"answers": []}))
        answers, _ = answers_mod.build_form_answers(
            preference=self._preference(work_authorization="US citizen, no sponsorship needed"),
            resume_json={},
            posting=_posting(),
        )
        work_auth = next(a for a in answers if a["key"] == "work_authorization")
        assert work_auth["answer"] == "US citizen, no sponsorship needed"
        assert work_auth["provenance"] == "preference:work_authorization"

    def test_a_fact_key_needs_a_preference_source(self, monkeypatch):
        """salary/notice may be *phrased* by the model, but only from a stated
        preference -- a resume citation is the same fabrication in a smaller
        font."""
        payload = {
            "answers": [
                {
                    "key": "salary_expectation",
                    "answer": "Around $180k, based on my seniority.",
                    "provenance": "resume_json.experience[0].title",
                },
                {
                    "key": "notice_period",
                    "answer": "Two weeks.",
                    "provenance": "preference:timeline",
                },
            ]
        }
        monkeypatch.setattr(answers_mod, "get_llm", lambda: _FakeLLM(payload))
        answers, _ = answers_mod.build_form_answers(
            preference=self._preference(timeline="two weeks"),
            resume_json={},
            posting=_posting(),
        )
        salary = next(a for a in answers if a["key"] == "salary_expectation")
        notice = next(a for a in answers if a["key"] == "notice_period")
        assert salary["answer"] == "", "resume-cited comp guess must be discarded"
        assert notice["answer"] == "Two weeks.", "preference-cited phrasing is allowed"


class TestWhyCompanyIsDraftedForReview:
    def test_the_draft_lands_flagged(self, monkeypatch):
        payload = {
            "answers": [
                {
                    "key": "why_company",
                    "answer": "My time at CrowdVolt maps directly onto this stack.",
                    "provenance": "resume_json.experience[1]; posting.description",
                }
            ]
        }
        monkeypatch.setattr(answers_mod, "get_llm", lambda: _FakeLLM(payload))
        answers, _ = answers_mod.build_form_answers(
            preference=None, resume_json={}, posting=_posting()
        )
        why = next(a for a in answers if a["key"] == "why_company")
        assert why["answer"].startswith("My time at CrowdVolt")
        assert why["needs_review"] is True
        assert why["generated_by"] == "llm"
        assert why["edited"] is False

    def test_no_model_means_blank_not_template_prose(self, monkeypatch):
        monkeypatch.setattr(
            answers_mod, "get_llm", lambda: _FakeLLM(None, available=False)
        )
        answers, generated_by = answers_mod.build_form_answers(
            preference=None, resume_json={}, posting=_posting()
        )
        why = next(a for a in answers if a["key"] == "why_company")
        assert why["answer"] == ""
        assert generated_by == "template"

    def test_the_posting_reaches_the_prompt(self, monkeypatch):
        llm = _FakeLLM({"answers": []})
        monkeypatch.setattr(answers_mod, "get_llm", lambda: llm)
        answers_mod.build_form_answers(
            preference=None,
            resume_json={},
            posting=_posting(jd_text="We build robots for shipyards."),
        )
        assert "We build robots for shipyards." in llm.prompts[0]

    def test_every_entry_carries_its_origin(self, monkeypatch):
        monkeypatch.setattr(answers_mod, "get_llm", lambda: _FakeLLM({"answers": []}))
        answers, _ = answers_mod.build_form_answers(
            preference=None, resume_json={}, posting=_posting()
        )
        assert all(a["origin"] == "standard" for a in answers)


class TestCleanTitle:
    from juicebox.presentation.jobcard import clean_title, role_line

    def test_a_trailing_company_clause_is_stripped(self):
        from juicebox.presentation.jobcard import clean_title

        title = "Junior Software Developer job in Houston at Wipro Limited"
        assert clean_title(title, "Wipro Limited") == (
            "Junior Software Developer job in Houston"
        )

    def test_mid_title_mentions_stay(self):
        from juicebox.presentation.jobcard import clean_title

        assert clean_title("Wipro Limited Graduate Programme", "Wipro Limited") == (
            "Wipro Limited Graduate Programme"
        )

    def test_role_line_never_doubles(self):
        from juicebox.presentation.jobcard import role_line

        doubled = "Junior Software Developer at Wipro Limited"
        assert role_line(doubled, "Wipro Limited") == doubled
        assert role_line("Founding Engineer", "Deep24") == "Founding Engineer at Deep24"
        assert role_line("Founding Engineer", "") == "Founding Engineer"
