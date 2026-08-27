"""Registry cutover: public forms need the agent; keyed APIs and two-leg stay."""

from __future__ import annotations

import pytest

from juicebox.applications.ats import detect
from juicebox.applications.submit import registry
from juicebox.applications.submit.browser.agent import BrowserAgentSubmitter
from juicebox.applications.submit.browser.cloud import FakeCloudClient, set_cloud_client
from juicebox.config import reset_settings_cache


@pytest.fixture
def live_mode(monkeypatch):
    monkeypatch.setenv("APPLY_SUBMITTER", "live")
    reset_settings_cache()
    registry.reset_overrides()
    yield
    registry.reset_overrides()
    reset_settings_cache()


@pytest.fixture
def agent_on(live_mode, monkeypatch):
    monkeypatch.setenv("BROWSER_USE_API_KEY", "bu_test")
    monkeypatch.setenv("APPLY_BROWSER_AGENT", "true")
    monkeypatch.setenv("APPLY_BROWSER_AGENT_ACK", "true")
    reset_settings_cache()
    fake = FakeCloudClient()
    set_cloud_client(fake)
    yield fake
    set_cloud_client(None)
    reset_settings_cache()


class TestRegistryCutover:
    def test_no_key_greenhouse_is_handoff_under_live(self, live_mode):
        target = detect.detect_target("https://boards.greenhouse.io/anthropic/jobs/4012345")
        assert registry.get_submitter(target) is None
        assert not registry.can_submit_vendor("greenhouse")

    def test_agent_configured_uses_the_agent(self, agent_on):
        target = detect.detect_target("https://boards.greenhouse.io/anthropic/jobs/4012345")
        chosen = registry.get_submitter(target)
        assert isinstance(chosen, BrowserAgentSubmitter)
        assert chosen.method == "browser"
        assert registry.can_submit_vendor("greenhouse")
        assert registry.can_submit_vendor("taleo")
        assert registry.can_submit_vendor("")
        assert registry.can_submit_vendor("icims")

    def test_board_key_still_selects_the_api(self, agent_on):
        from juicebox.applications.submit.greenhouse import GreenhouseSubmitter

        target = detect.detect_target("https://boards.greenhouse.io/anthropic/jobs/4012345")
        chosen = registry.get_submitter(target, board_key="gh-key")
        assert isinstance(chosen, GreenhouseSubmitter)
        assert chosen.board_key == "gh-key"

    def test_account_gated_stays_out_of_the_agent(self, agent_on):
        from juicebox.applications.submit.browser.workday import WorkdaySubmitter
        from juicebox.applications.submit.browser.yc import YCSubmitter

        workday = detect.ApplyTarget(vendor="workday", apply_url="https://x.myworkdayjobs.com/en-US/x")
        assert isinstance(registry.get_submitter(workday), WorkdaySubmitter)
        yc = detect.ApplyTarget(vendor="yc", apply_url="https://www.workatastartup.com/jobs/x")
        assert isinstance(registry.get_submitter(yc), YCSubmitter)
        oracle = detect.ApplyTarget(vendor="oracle", apply_url="https://x.oraclecloud.com")
        assert registry.get_submitter(oracle) is None
        assert not registry.can_submit_vendor("oracle")

    def test_can_submit_vendor_ignores_apply_submitter_mode(self, agent_on, monkeypatch):
        monkeypatch.setenv("APPLY_SUBMITTER", "console")
        reset_settings_cache()
        assert registry.can_submit_vendor("greenhouse")
        assert registry.mode() == "console"

    def test_unknown_board_without_agent_is_handoff(self, live_mode):
        target = detect.ApplyTarget(vendor="", apply_url="https://careers.tesla.com/x")
        assert registry.get_submitter(target) is None
        assert not registry.can_submit_vendor("")
