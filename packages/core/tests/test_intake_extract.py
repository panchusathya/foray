

class TestCompIsNeverInvented:
    """A comp figure has to come from the conversation.

    From a live thread whose entire inbound content was a greeting, an email
    address, a LinkedIn URL and "strategic finance analyst roles": extraction
    produced target_base = 1,200,000. Nothing said it. That figure then vetoed
    every real posting on comp_floor, so the candidate saw no roles at all and
    nothing on screen explained why. An invented number is worse than a
    missing one exactly because it behaves like a real answer.
    """

    LIVE = (
        "Hey Hello Panchu.sathya@gmail.com "
        "https://www.linkedin.com/in/sathyapanchu?utm_source=share_via "
        "No I want to switch to Strat fin Strategic finance analyst roles"
    )

    def test_the_live_transcript_names_no_figure(self):
        from juicebox.intake.extract import mentions_a_figure

        assert not mentions_a_figure(self.LIVE)

    def test_digits_inside_a_link_or_address_are_not_money(self):
        from juicebox.intake.extract import mentions_a_figure

        assert not mentions_a_figure("see https://jobs.example.com/12345")
        assert not mentions_a_figure("reach me at a1b2@example.com")

    def test_a_real_figure_is_recognised(self):
        from juicebox.intake.extract import mentions_a_figure

        assert mentions_a_figure("I'm targeting 150k base")
        assert mentions_a_figure("around $200,000 total")

    def test_an_uncited_figure_is_dropped(self):
        from juicebox.intake.extract import _drop_uncited_comp

        record = {"target_base": 1_200_000.0, "target_total": None}
        _drop_uncited_comp(record, self.LIVE)
        assert record["target_base"] is None
        assert record["target_total"] is None

    def test_a_cited_figure_survives(self):
        """The guard must not eat real answers."""
        from juicebox.intake.extract import _drop_uncited_comp

        record = {"target_base": 150_000.0, "target_total": 180_000.0}
        _drop_uncited_comp(record, "150k base, 180 all in")
        assert record["target_base"] == 150_000.0
        assert record["target_total"] == 180_000.0

    def test_extraction_drops_it_end_to_end(self, monkeypatch):
        """The model returns the figure; extraction must not keep it."""
        from types import SimpleNamespace

        import juicebox.intake.extract as mod

        class FakeLLM:
            available = True

            def json(self, **kwargs):
                return SimpleNamespace(
                    data={"target_base": 1200000, "job_titles": ["strategic finance analyst"]}
                )

            json_fast = json

        monkeypatch.setattr(mod, "get_llm", lambda: FakeLLM())
        result = mod.extract_intake(self.LIVE, known="")
        assert result.record["target_base"] is None, "invented comp reached the record"
        assert result.extractor == "llm", "the guard must not force a heuristic fallback"
        # Only comp is touched -- the record is still a whole record.
        assert "spec" in result.record and "timeline" in result.record

    def test_a_cited_figure_survives_extraction(self, monkeypatch):
        """The same path, with the number actually said out loud."""
        from types import SimpleNamespace

        import juicebox.intake.extract as mod

        class FakeLLM:
            available = True

            def json(self, **kwargs):
                return SimpleNamespace(data={"target_base": 150000})

            json_fast = json

        monkeypatch.setattr(mod, "get_llm", lambda: FakeLLM())
        result = mod.extract_intake("I'm looking for 150k base", known="")
        assert result.record["target_base"] == 150000.0


class TestDegradedExtractionIsVisible:
    """A failed extractor and an unconfigured one produced identical records.

    That is why "how often is the coverage gate running on regexes?" had no
    answer: both looked like `EXTRACTOR_HEURISTIC` from the outside, and the
    only trace of a timeout was a warning nobody counted.
    """

    def _llm(self, *, available=True, raises=False):
        from types import SimpleNamespace

        class FakeLLM:
            def __init__(self):
                self.available = available
                self.voice_timeout = 3.5
                self.seen = {}

            def json_fast(self, **kwargs):
                self.seen = kwargs
                if raises:
                    raise RuntimeError("timed out")
                return SimpleNamespace(data={"spec": {"job_titles": ["backend engineer"]}})

            json = json_fast

        return FakeLLM()

    def test_a_failed_extractor_is_not_mistaken_for_a_missing_one(self, monkeypatch):
        import juicebox.intake.extract as mod

        monkeypatch.setattr(mod, "get_llm", lambda: self._llm(raises=True))
        result = mod.extract_intake("Candidate: hello", known="", fast=True)

        assert result.extractor == mod.EXTRACTOR_HEURISTIC_DEGRADED
        assert result.extractor != mod.EXTRACTOR_HEURISTIC
        assert result.review_needed, "a degraded read must not look clean"
        assert "heuristics" in result.review_reason

    def test_no_model_configured_is_still_a_plain_heuristic_read(self, monkeypatch):
        """Not a fault: a deployment with no key is a supported configuration."""
        import juicebox.intake.extract as mod

        monkeypatch.setattr(mod, "get_llm", lambda: self._llm(available=False))
        result = mod.extract_intake("Candidate: hello", known="", fast=True)
        assert result.extractor == mod.EXTRACTOR_HEURISTIC


class TestTheVoiceBudgetIsSeparate:
    """One shared timeout was either too slow for a live call or too tight for a
    text thread, and it was both."""

    def _llm(self):
        from types import SimpleNamespace

        class FakeLLM:
            available = True
            voice_timeout = 3.5

            def __init__(self):
                self.seen = {}

            def json_fast(self, **kwargs):
                self.seen = kwargs
                return SimpleNamespace(data={"spec": {"job_titles": ["backend engineer"]}})

            json = json_fast

        return FakeLLM()

    def test_a_live_call_gets_the_short_budget(self, monkeypatch):
        import juicebox.intake.extract as mod

        llm = self._llm()
        monkeypatch.setattr(mod, "get_llm", lambda: llm)
        mod.extract_intake("Candidate: hi", known="", fast=True, live_call=True)
        assert llm.seen.get("timeout") == 3.5

    def test_a_text_thread_keeps_the_general_budget(self, monkeypatch):
        """No override: `json_fast` applies the longer configured default."""
        import juicebox.intake.extract as mod

        llm = self._llm()
        monkeypatch.setattr(mod, "get_llm", lambda: llm)
        mod.extract_intake("Candidate: hi", known="", fast=True)
        assert "timeout" not in llm.seen


class TestHeuristicIgnoresTheAgent:
    def test_the_agents_script_does_not_fill_preferences(self):
        """Voice used to run regexes over the full transcript. The greeting
        names the roles we recruit for; that is not an answer."""
        from juicebox.intake.extract import EXTRACTOR_HEURISTIC, extract_intake

        result = extract_intake(
            "Agent: Hi, I'm looking for senior backend engineers in New York "
            "who want fully remote roles.\n"
            "Candidate: just my name is Priya\n",
            use_llm=False,
        )
        assert result.extractor == EXTRACTOR_HEURISTIC
        assert result.record["spec"]["job_titles"] == []
        assert result.record["spec"]["seniority"] == []
        assert result.record["spec"].get("remote_ok") is None
        assert result.record["spec"].get("locations") in ([], None)
