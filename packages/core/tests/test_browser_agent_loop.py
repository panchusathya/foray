"""Local loop fixtures: scripted planner, fake page, no API key required for the
planner itself. Cloud is injected so create/stop still run."""

from __future__ import annotations

import pytest

from juicebox.applications.ats.detect import ApplyTarget
from juicebox.applications.submit.base import SubmissionContext
from juicebox.applications.submit.browser.agent import loop
from juicebox.applications.submit.browser.agent.actions import Action
from juicebox.applications.submit.browser.agent.observe import Control
from juicebox.applications.submit.browser.cloud import FakeCloudClient, set_cloud_client
from juicebox.config import reset_settings_cache


def _ctx(**kwargs):
    base = dict(
        application_id="app-1",
        org_id="org-1",
        candidate_id="cand-1",
        target=ApplyTarget(vendor="greenhouse", apply_url="https://boards.greenhouse.io/x/jobs/1"),
        full_name="Ada Okonkwo",
        first_name="Ada",
        last_name="Okonkwo",
        from_address="ada@example.com",
        answers=[],
        posting_url="https://boards.greenhouse.io/x/jobs/1",
    )
    base.update(kwargs)
    return SubmissionContext(**base)


class FakeLocator:
    def __init__(self, page, control_id):
        self.page = page
        self.control_id = control_id

    def fill(self, value):
        self.page.filled[self.control_id] = value

    def click(self):
        self.page.clicked.append(self.control_id)
        if self.control_id in self.page.submit_ids:
            self.page.submitted = True

    def check(self):
        self.page.checked.append(self.control_id)

    def select_option(self, *a, **kw):
        self.page.filled[self.control_id] = a[0] if a else kw.get("label", "")

    def set_input_files(self, path):
        self.page.uploaded[self.control_id] = path

    @property
    def first(self):
        return self


class FakePage:
    def __init__(self, *, url, controls, receipt_after_submit=True, body="Apply now"):
        self.url = url
        self._title = "Apply"
        self._body = body
        self.controls = list(controls)
        self.filled = {}
        self.clicked = []
        self.checked = []
        self.uploaded = {}
        self.submitted = False
        self.submit_ids = {c.id for c in controls if c.role == "submit" or "submit" in c.label.lower()}
        self.receipt_after_submit = receipt_after_submit
        self.screenshots = 0

    def title(self):
        return self._title

    def inner_text(self, _sel):
        if self.submitted and self.receipt_after_submit:
            return "Thank you for applying. We have received your application."
        return self._body

    def evaluate(self, _js):
        if self.submitted and self.receipt_after_submit:
            return []
        return [
            {
                "id": c.id,
                "label": c.label,
                "fieldType": c.field_type,
                "role": c.role,
                "name": c.name,
                "required": c.required,
                "options": list(c.options),
            }
            for c in self.controls
        ]

    def screenshot(self, full_page=True):
        self.screenshots += 1
        return b"png-bytes"

    def locator(self, selector):
        cid = selector.split('"')[1] if '"' in selector else ""
        return FakeLocator(self, cid)

    def goto(self, url, wait_until=""):
        self.url = url

    def wait_for_timeout(self, _ms):
        return None


class ScriptedPlanner:
    def __init__(self, actions: list[Action | dict]):
        self._queue = list(actions)
        self.calls = 0

    def json(self, **kwargs):
        from juicebox.llm.client import LLMResult

        self.calls += 1
        if not self._queue:
            return LLMResult(text='{"type":"stop"}', data={"type": "stop"}, model="x", stop_reason="end")
        item = self._queue.pop(0)
        data = item if isinstance(item, dict) else {
            "type": item.type, "control_id": item.control_id, "value_token": item.value_token,
        }
        return LLMResult(text="", data=data, model="x", stop_reason="end")


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_API_KEY", "bu_test")
    reset_settings_cache()
    fake = FakeCloudClient()
    set_cloud_client(fake)
    yield fake
    set_cloud_client(None)
    reset_settings_cache()


def _patch_driver(monkeypatch, page: FakePage):
    import contextlib

    from juicebox.applications.submit.browser import driver

    @contextlib.contextmanager
    def fake_session(*, log, profile=None, cdp_url="", cloud_stop_id=""):
        log.step("browser opened (managed chrome)")
        yield page
        log.step("browser closed")

    def fake_snapshot(page, *, org_id, application_id, label, log=None):
        path = f"{org_id}/applications/{application_id}/{label}.png"
        if log is not None:
            log.artifacts.append(path)
        return path

    monkeypatch.setattr(driver, "session", fake_session)
    monkeypatch.setattr(driver, "snapshot", fake_snapshot)
    monkeypatch.setattr(driver, "available", lambda: (True, ""))


CONTROLS = (
    Control(id="c001", label="First Name", field_type="text", name="first_name"),
    Control(id="c002", label="Email", field_type="email", name="email"),
    Control(id="c003", label="Resume", field_type="file", name="resume"),
    Control(id="c004", label="Submit Application", field_type="submit", role="submit"),
)


class TestLoop:
    def test_one_page_submit_confirms(self, cloud, monkeypatch, tmp_path):
        page = FakePage(url="https://boards.greenhouse.io/x/jobs/1", controls=CONTROLS)
        _patch_driver(monkeypatch, page)
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF")
        from juicebox.applications.submit.base import Attachment

        planner = ScriptedPlanner([
            Action(type="fill", control_id="c001", value_token="candidate.first_name"),
            Action(type="fill", control_id="c002", value_token="candidate.email"),
            Action(type="upload", control_id="c003", value_token="doc.resume"),
            Action(type="final_submit", control_id="c004"),
        ])
        ctx = _ctx(resume=Attachment(filename="resume.pdf", content_type="application/pdf", content=b"%PDF"))
        result = loop.run(ctx, allow_submit=True, planner=planner)
        assert result.ok
        assert not result.needs_human
        assert page.filled["c002"] == "ada@example.com"
        assert "c004" in page.clicked
        assert cloud.stopped
        assert result.session_id
        assert result.final_submit_requested_at
        assert loop.confirmation_artifact(result.artifacts)

    def test_preflight_cannot_submit(self, cloud, monkeypatch):
        page = FakePage(url="https://boards.greenhouse.io/x/jobs/1", controls=CONTROLS)
        _patch_driver(monkeypatch, page)
        planner = ScriptedPlanner([Action(type="final_submit", control_id="c004")])
        result = loop.run(_ctx(), allow_submit=False, planner=planner)
        assert not result.ok
        assert result.needs_human
        assert "c004" not in page.clicked
        assert cloud.stopped

    def test_preflight_collects_questions_without_an_attempt(self, cloud, monkeypatch):
        page = FakePage(
            url="https://boards.greenhouse.io/x/jobs/1",
            controls=(*CONTROLS, Control(id="c010", label="How did you hear about us?", field_type="text", required=True)),
        )
        _patch_driver(monkeypatch, page)
        planner = ScriptedPlanner([Action(type="stop", reason="preflight done")])
        result = loop.run(_ctx(application_id=""), allow_submit=False, planner=planner)
        assert not result.ok
        assert any(c.label == "How did you hear about us?" for c in result.questions)

    def test_ambiguous_after_submit_is_handoff_not_retry(self, cloud, monkeypatch, tmp_path):
        page = FakePage(
            url="https://boards.greenhouse.io/x/jobs/1",
            controls=CONTROLS,
            receipt_after_submit=False,
        )
        _patch_driver(monkeypatch, page)
        planner = ScriptedPlanner([
            Action(type="final_submit", control_id="c004"),
            Action(type="final_submit", control_id="c004"),
        ])
        result = loop.run(_ctx(), allow_submit=True, planner=planner)
        assert not result.ok
        assert result.needs_human
        assert "ambiguous" in result.error
        assert page.clicked.count("c004") == 1
        assert any(str(p).endswith("unconfirmed.png") or "/unconfirmed" in str(p) for p in result.artifacts)
        assert not loop.confirmation_artifact(result.artifacts)

    def test_receipt_without_our_submit_is_handoff(self, cloud, monkeypatch):
        page = FakePage(
            url="https://boards.greenhouse.io/x/jobs/1",
            controls=(),
            body="Thank you for applying. We have received your application.",
        )
        _patch_driver(monkeypatch, page)
        planner = ScriptedPlanner([])
        result = loop.run(_ctx(), allow_submit=True, planner=planner)
        assert not result.ok
        assert "receipt page before any submit" in result.error

    def test_recover_does_not_click(self, cloud, monkeypatch):
        page = FakePage(
            url="https://boards.greenhouse.io/x/jobs/1",
            controls=(),
            body="Thank you for applying. We have received your application.",
        )
        page.submitted = True
        _patch_driver(monkeypatch, page)
        session = cloud.create_browser({})
        ctx = _ctx()
        result = loop.recover(
            ctx,
            {"run": {"provider_session_id": session.id, "final_submit_requested_at": "now"}},
        )
        assert result.ok
        assert page.clicked == []
        assert session.id in cloud.stopped

    def test_recover_stopped_session_does_not_click(self, cloud, monkeypatch):
        page = FakePage(
            url="https://boards.greenhouse.io/x/jobs/1",
            controls=CONTROLS,
        )
        _patch_driver(monkeypatch, page)
        session = cloud.create_browser({})
        cloud.stop_browser(session.id)
        result = loop.recover(
            _ctx(),
            {"run": {"provider_session_id": session.id, "final_submit_requested_at": "now"}},
        )
        assert not result.ok
        assert result.needs_human
        assert page.clicked == []
        assert "session gone" in result.error


class TestConfirmationArtifact:
    def test_picks_the_labelled_shot(self):
        assert loop.confirmation_artifact(
            ["org/applications/a/before-submit.png", "org/applications/a/confirmation.png"]
        ).endswith("confirmation.png")

    def test_ignores_unconfirmed_and_before(self):
        assert loop.confirmation_artifact(["org/applications/a/before-submit.png"]) == ""
        assert loop.confirmation_artifact(["org/applications/a/unconfirmed.png"]) == ""


class TestFormRoute:
    """Boards that split overview from form get the form route, deterministically."""

    def test_ashby_overview_gets_the_application_route(self):
        from juicebox.applications.submit.browser.agent.loop import form_route

        assert form_route(
            "https://jobs.ashbyhq.com/forerunner/1a9c649b-dd5b?ref=reactjobs.io"
        ) == "https://jobs.ashbyhq.com/forerunner/1a9c649b-dd5b/application"

    def test_an_ashby_application_route_is_left_alone(self):
        from juicebox.applications.submit.browser.agent.loop import form_route

        url = "https://jobs.ashbyhq.com/forerunner/1a9c649b-dd5b/application"
        assert form_route(url) == url

    def test_other_hosts_are_untouched(self):
        from juicebox.applications.submit.browser.agent.loop import form_route

        url = "https://careers.example.com/jobs/5?utm=x"
        assert form_route(url) == url
