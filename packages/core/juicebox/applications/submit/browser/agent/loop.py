"""The observe → plan → policy → execute loop.

``allow_submit`` is the only difference between preflight and a live attempt.
Preflight never consumes an ApplicationSubmission row; the live run starts a
fresh cloud session after answers exist.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

from .....config import get_settings
from ...base import SubmissionContext, SubmitDeferred
from .. import cloud as browser_cloud
from .. import driver
from ..driver import RunLog
from . import execute as execute_mod
from . import planner as planner_mod
from . import policy, trace
from .actions import Action
from .observe import Control, Observation, observe
from .policy import PAGE_RECEIPT, Decision, page_class, required_blanks

logger = logging.getLogger(__name__)

CONFIRMATION_LABEL = "confirmation"


class LoopResult:
    def __init__(self) -> None:
        self.ok = False
        self.needs_human = False
        self.error = ""
        self.artifacts: list[str] = []
        self.steps: list[dict] = []
        self.questions: list[Control] = []
        self.session_id = ""
        self.final_submit_requested_at = ""
        self.page_class = ""


def form_route(url: str) -> str:
    """The route where the form actually lives, for boards that split it.

    Ashby's job URL is an overview page — the application form is its
    ``/application`` sub-route, and the agent has no navigate action on
    purpose, so it inventoried zero controls on the overview and stopped
    (live, Forerunner 2026-08-27). Deterministic and vendor-stated, never the
    planner's guess.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.netloc.lower() == "jobs.ashbyhq.com":
        path = parsed.path.rstrip("/")
        if path and not path.endswith("/application"):
            return parsed._replace(path=path + "/application", query="", fragment="").geturl()
    return url


_LOADING_RE = re.compile(
    r"fetching application form|loading[.…]|please wait", re.IGNORECASE
)

#: Enumeration counts page chrome — nav links, cookie buttons — so "controls
#: exist" is not "the form exists". A spinner page with a nav bar passed the
#: first version of this gate in 0ms while the spinner text sat in a
#: pseudo-element that ``inner_text`` cannot see (the planner read it off the
#: screenshot). Only these field types are chrome; anything else is a form.
_CHROME_TYPES = frozenset({"button", "submit", "reset", "image", "a", "link"})


def _has_form_controls(obs) -> bool:
    return any(c.field_type not in _CHROME_TYPES for c in obs.controls)


def _settled_observe(page, *, timeout_ms: int = 30000):
    """Observe once the client-rendered form actually exists.

    ``domcontentloaded`` fires on the shell; Ashby then shows "Fetching
    application form" while the controls stream in, and the planner read that
    screen as a formless page and stopped (live, Forerunner 2026-08-27,
    twice). Every iteration settles — a click_next on a multi-step form
    re-renders the same way the first load does. The gate is a *form-ish*
    control (chrome buttons and links do not count), with a receipt page
    returned immediately — a thank-you page never grows an input. Waiting is
    deterministic loop code, never a planner judgment; on timeout the last
    observation is returned and the planner says what it honestly sees.
    """
    waited = 0
    obs = observe(page)
    while waited < timeout_ms:
        if page_class(obs) == PAGE_RECEIPT:
            return obs
        if _has_form_controls(obs) and not _LOADING_RE.search(obs.body_text or ""):
            return obs
        page.wait_for_timeout(500)
        waited += 500
        obs = observe(page)
    return obs


def run(
    ctx: SubmissionContext,
    *,
    allow_submit: bool,
    planner=None,
    start_url: str = "",
) -> LoopResult:
    result = LoopResult()
    settings = get_settings()
    url = form_route(start_url or ctx.target.apply_url or ctx.posting_url or "")
    if not url:
        result.needs_human = True
        result.error = "no apply url"
        return result
    start_origin = policy.origin_of(url)
    blanks = required_blanks(ctx) if allow_submit else []
    if blanks and allow_submit:
        result.needs_human = True
        result.error = "required questions unanswered: " + "; ".join(blanks[:5])
        return result

    log = RunLog()
    resume_path, cover_path, tmp = _doc_paths(ctx)
    session = None
    try:
        session = _open_session(ctx, start_origin=start_origin)
        result.session_id = session.id if session is not None else ""
        _checkpoint(ctx, result)
        with driver.session(
            log=log,
            cdp_url=(session.cdp_url if session is not None else ""),
        ) as page:
            page.goto(url, wait_until="domcontentloaded")
            max_steps = max(1, int(settings.apply_browser_agent_max_steps or 40))
            submitted = False
            planner_faults = 0
            for _ in range(max_steps):
                obs = _settled_observe(page)
                klass = page_class(obs)
                result.page_class = klass
                _collect_questions(result, obs)
                if klass == PAGE_RECEIPT:
                    if submitted or not allow_submit:
                        result.ok = bool(allow_submit) and submitted
                        if allow_submit and submitted:
                            _confirmation_shot(page, ctx, log, result)
                        else:
                            result.needs_human = True
                            result.error = "receipt page without a submit we requested"
                        break
                    result.needs_human = True
                    result.error = "receipt page before any submit"
                    break
                action = planner_mod.plan(
                    obs,
                    allow_submit=allow_submit,
                    approved_answer_keys=_approved_keys(ctx),
                    planner=planner,
                )
                if action is None:
                    result.needs_human = True
                    result.error = "planner returned no action"
                    _trace(result, obs, None, None, "none")
                    break
                decision = policy.check(
                    action,
                    obs=obs,
                    ctx=ctx,
                    allow_submit=allow_submit,
                    start_origin=start_origin,
                    resume_path=resume_path,
                    cover_path=cover_path,
                )
                if not decision.allowed:
                    if decision.reason == "unknown control_id" and planner_faults < 3:
                        # A planner mistake, not a governance wall: it named a
                        # control that does not exist (live: an empty id on
                        # step two killed a run whose step one had just filled
                        # correctly). Re-observe and re-plan, bounded — a
                        # governance refusal still ends the run below.
                        planner_faults += 1
                        _trace(result, obs, action, decision, "replanned")
                        continue
                    result.needs_human = True
                    result.error = decision.reason
                    _trace(result, obs, action, decision, "refused")
                    break
                if action.type == "stop":
                    result.needs_human = True
                    result.error = action.reason or f"stopped on {klass}"
                    _trace(result, obs, action, decision, "stop")
                    break
                if action.type == "final_submit":
                    result.final_submit_requested_at = trace.utcstamp()
                    _checkpoint(ctx, result)
                    _before_shot(page, ctx, log, result)
                try:
                    executed = execute_mod.execute(page, action, decision)
                except Exception as exc:  # noqa: BLE001
                    result.needs_human = True
                    result.error = f"execute failed: {exc}"[:400]
                    _trace(result, obs, action, decision, "error")
                    break
                _trace(result, obs, action, decision, executed)
                if action.type == "final_submit":
                    submitted = True
                    page.wait_for_timeout(2500)
                    after = observe(page)
                    after_class = page_class(after)
                    result.page_class = after_class
                    if after_class == PAGE_RECEIPT:
                        result.ok = True
                        _confirmation_shot(page, ctx, log, result)
                    else:
                        result.needs_human = True
                        result.error = "ambiguous state after submit"
                        _shot(page, ctx, log, result, "unconfirmed")
                    break
            else:
                result.needs_human = True
                result.error = "step cap reached"
        result.artifacts.extend(log.artifacts)
    except browser_cloud.TooManySessions:
        raise SubmitDeferred("too many concurrent Browser Use sessions") from None
    except browser_cloud.CloudError as exc:
        result.needs_human = True
        result.error = str(exc)[:400]
    except driver.BrowserUnavailable as exc:
        result.needs_human = True
        result.error = str(exc)[:400]
    finally:
        tmp.cleanup()
        if session is not None and allow_submit:
            # Live run: stop unless a checkpointed submit is still in flight
            # on this session and we did not finish inspecting it. The worker
            # recover path owns stop in that case. Here we always stop after
            # a completed loop (ok, human, or error).
            browser_cloud.stop_session(session.id)
        elif session is not None:
            browser_cloud.stop_session(session.id)
    return result


def recover(ctx: SubmissionContext, verification: dict) -> LoopResult:
    """Inspect a session that already had final_submit requested. Never click."""
    result = LoopResult()
    run_state = (verification or {}).get("run") or verification or {}
    session_id = str(run_state.get("provider_session_id") or "")
    result.session_id = session_id
    result.final_submit_requested_at = str(run_state.get("final_submit_requested_at") or "")
    result.steps = list(run_state.get("steps") or [])
    if not session_id:
        result.needs_human = True
        result.error = "no provider session to inspect"
        return result
    held = browser_cloud.get_session(session_id)
    log = RunLog()
    try:
        if held is None or held.status != "active" or not held.cdp_url:
            result.needs_human = True
            result.error = "session gone after submit; inspect artifacts"
            return result
        with driver.session(log=log, cdp_url=held.cdp_url) as page:
            obs = observe(page)
            klass = page_class(obs)
            result.page_class = klass
            if klass == PAGE_RECEIPT:
                result.ok = True
                _confirmation_shot(page, ctx, log, result)
            else:
                result.needs_human = True
                result.error = "ambiguous state after submit"
                _shot(page, ctx, log, result, "unconfirmed")
        result.artifacts.extend(log.artifacts)
    finally:
        browser_cloud.stop_session(session_id)
    return result


def _open_session(ctx: SubmissionContext, *, start_origin: str):
    if not browser_cloud.configured():
        return None
    profile_id = ""
    if ctx.browser_profile and getattr(ctx.browser_profile, "cloud_profile_id", ""):
        profile_id = ctx.browser_profile.cloud_profile_id
    metadata = {
        "org_id": ctx.org_id,
        "application_id": ctx.application_id,
        "phase": "live" if ctx.application_id else "preflight",
    }
    if start_origin:
        metadata["origin"] = start_origin[:200]
    return browser_cloud.create_session(profile_id=profile_id, metadata=metadata)


def _checkpoint(ctx: SubmissionContext, result: LoopResult) -> None:
    if not callable(ctx.persist_checkpoint):
        return
    ctx.persist_checkpoint(
        trace.payload(
            session_id=result.session_id,
            final_submit_requested_at=result.final_submit_requested_at,
            steps=result.steps,
            artifacts=result.artifacts,
        )
    )


def _trace(
    result: LoopResult,
    obs: Observation,
    action: Action | None,
    decision: Decision | None,
    executed: str,
) -> None:
    result.steps.append(
        trace.record(
            observation_fingerprint=obs.fingerprint(),
            url=obs.url,
            page_class=page_class(obs),
            proposed=action,
            decision=decision,
            executed=executed,
        )
    )


def _collect_questions(result: LoopResult, obs: Observation) -> None:
    for control in obs.controls:
        if control.field_type in {"password"}:
            continue
        if control.role in {"button", "submit", "link"}:
            continue
        if any(c.id == control.id for c in result.questions):
            continue
        result.questions.append(control)


def confirmation_artifact(artifacts: list[str]) -> str:
    """The receipt screenshot path, if one was stored under the confirmation label."""
    for path in artifacts or []:
        name = str(path).rsplit("/", 1)[-1]
        if name.startswith(CONFIRMATION_LABEL):
            return str(path)
    return ""


def _approved_keys(ctx: SubmissionContext) -> tuple[str, ...]:
    return tuple(
        str(entry.get("key"))
        for entry in ctx.answers
        if str(entry.get("key") or "").strip() and str(entry.get("answer") or "").strip()
    )


def _doc_paths(ctx: SubmissionContext):
    tmp = tempfile.TemporaryDirectory(prefix="jb-apply-")
    resume = cover = ""
    if ctx.resume is not None:
        path = Path(tmp.name) / (ctx.resume.filename or "resume.pdf")
        path.write_bytes(ctx.resume.content)
        resume = str(path)
    if ctx.cover_letter is not None:
        path = Path(tmp.name) / (ctx.cover_letter.filename or "cover.pdf")
        path.write_bytes(ctx.cover_letter.content)
        cover = str(path)
    return resume, cover, tmp


def _confirmation_shot(page, ctx, log, result: LoopResult) -> None:
    _shot(page, ctx, log, result, CONFIRMATION_LABEL)


def _before_shot(page, ctx, log, result: LoopResult) -> None:
    _shot(page, ctx, log, result, "before-submit")


def _shot(page, ctx, log, result: LoopResult, label: str) -> None:
    if not ctx.org_id or not ctx.application_id:
        return
    path = driver.snapshot(
        page, org_id=ctx.org_id, application_id=ctx.application_id, label=label, log=log
    )
    if path:
        result.artifacts.append(path)
