"""One Chromium, serialised, with a record of what it did.

Modelled on ``enrich/linkedin_scrape.py``: the same module-level lock (two
threads must not share one browser), the same "playwright is not installed is a
normal answer, not a crash", and the same posture that a failure here degrades
rather than explodes.

What is different is the stakes. A scrape that goes wrong wastes a request; a
form fill that goes wrong puts words in somebody's name in front of an
employer, and cannot be withdrawn. So this module adds two things the scraper
does not have:

* **An artifact per run.** The final screenshot and the field map that was
  filled, written through ``storage`` and referenced from the attempt row. A
  submission nobody can reconstruct is one we cannot answer for.
* **A refusal to improvise.** ``fill`` reports which required fields it could
  not find, and the adapter turns that into ``needs_human``. There is no
  "click the most likely button" path.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .... import storage
from ....config import get_settings
from ..base import REDACTED
from . import cloud as browser_cloud

logger = logging.getLogger(__name__)

# Serialises Chromium across worker threads, and paces us at a rate a careers
# portal would not notice. Same reasoning as linkedin_scrape._RATE_LOCK.
_RATE_LOCK = threading.Lock()
_LAST_RUN_AT = 0.0
_MIN_INTERVAL_SECONDS = 4.0

NAV_TIMEOUT_MS = 45_000
ACTION_TIMEOUT_MS = 15_000
# A managed anti-detect browser (Bright Data Scraping Browser) is slower than a
# local Chromium: it may be solving a CAPTCHA or waiting out a challenge behind
# a single goto. Give the remote path room the local path does not need.
CDP_CONNECT_TIMEOUT_MS = 60_000
CDP_NAV_TIMEOUT_MS = 120_000


class BrowserUnavailable(RuntimeError):
    """Playwright or Chromium is not installed on this worker."""


@dataclass
class FieldSpec:
    """One form field: where it is, what goes in it, and whether we can skip it.

    ``selectors`` is tried in order, because a vendor ships several generations
    of the same form and the tenant decides which one you get. ``required``
    means a miss stops the run.
    """

    key: str
    selectors: tuple[str, ...]
    value: str = ""
    required: bool = False
    kind: str = "text"  # text | file | select | check | password | radio
    #: A credential. The run log records that the field was filled and never
    #: what went into it -- see ``_recorded``.
    secret: bool = False


@dataclass
class RunLog:
    """What happened, in the order it happened. Persisted with the attempt."""

    steps: list[str] = field(default_factory=list)
    filled: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)

    def step(self, message: str) -> None:
        logger.info("[apply-browser] %s", message)
        self.steps.append(message)

    def as_payload(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            # Values, not just keys: "we filled work_authorization with
            # 'Authorized, no sponsorship needed'" is the only version of this
            # record that answers a complaint.
            "filled": self.filled,
            "missing": self.missing,
        }


def _recorded(spec: FieldSpec) -> str:
    """What goes in ``RunLog.filled`` for a field we just filled."""
    if spec.kind == "file":
        return "<file>"
    if spec.kind == "password" or spec.secret:
        return REDACTED
    return spec.value


def available() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
    except ImportError as exc:
        return False, f"playwright is not installed ({exc})"
    return True, ""


#: Proxy gateways that encode a sticky-session id in the *username*, as
#: ``user-session-<token>``. Bright Data and most residential vendors do this.
#: A form fill is several requests to one domain, and they must all leave from
#: one IP -- a page loaded from IP A and submitted from IP B is a tell. A token
#: fixed for the life of one browser run gives each application its own stable
#: IP without pinning every application to a single one.
#:
#: Decodo (ex-Smartproxy): verified live 2026-08-27 — with the username
#: configured as ``user-<account>``, appending ``-session-<token>`` pins one
#: IP per token and overrides the gateway's port-based pin, so every run gets
#: its own residential IP. Two calls with one token returned one IP; a second
#: token returned another.
_SESSION_PROXY_HOSTS = ("superproxy.io", "decodo.com")


def _proxy_config(url: str, *, session_token: str = "") -> dict | None:
    """Playwright's proxy dict from a standard proxy URL, or None for direct.

    Playwright wants the server split from the credentials, so a URL with a
    userinfo part (``http://user:pass@host:port`` -- the shape every proxy
    vendor hands you) is broken back apart here. A bare ``host:port`` with no
    scheme is assumed http, which is what the endpoints use.

    ``session_token`` pins one sticky IP for this run: on a gateway that reads
    a session id out of the username, it is appended as ``-session-<token>``
    unless the configured username already carries one (so an operator who
    wants a fixed IP keeps it). Other proxies are untouched.
    """
    url = (url or "").strip()
    if not url:
        return None
    from urllib.parse import urlparse

    if "://" not in url:
        url = "http://" + url
    parsed = urlparse(url)
    # A hostname with whitespace is not a hostname -- it is a typo'd setting,
    # and launching through it would fail every run. Direct is the safer read.
    if not parsed.hostname or " " in parsed.hostname:
        return None
    scheme = parsed.scheme or "http"
    server = f"{scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    config: dict = {"server": server}
    username = parsed.username or ""
    if (
        username
        and session_token
        and "-session-" not in username
        and any(parsed.hostname.endswith(host) for host in _SESSION_PROXY_HOSTS)
    ):
        username = f"{username}-session-{session_token}"
    if username:
        config["username"] = username
    if parsed.password:
        config["password"] = parsed.password
    return config


@contextlib.contextmanager
def session(*, log: RunLog, profile=None, cdp_url: str = "", cloud_stop_id: str = ""):
    """A paced, serialised browser context. Closes whatever it opened.

    ``cdp_url`` is an already-open Cloud (or test) session the caller owns.
    When it is empty and Browser Use is configured, this context creates a
    session, connects, and **always** PATCH-stops it -- Playwright close does
    not end cloud billing. Callers that must reconnect after a crash (the
    agent, at the final-submit checkpoint) pass their own ``cdp_url`` and
    stop themselves.

    ``profile`` is a ``profiles.ProfileState`` when warm *local* profiles are
    on. Ignored on every CDP path: the managed browser owns the fingerprint,
    and overlaying UA / webdriver hacks is the tell.
    """
    ok, reason = available()
    if not ok:
        raise BrowserUnavailable(reason)
    from playwright.sync_api import sync_playwright

    global _LAST_RUN_AT
    with _RATE_LOCK:
        wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _LAST_RUN_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_RUN_AT = time.monotonic()

        settings = get_settings()
        import secrets

        session_token = (
            profile.egress_session
            if profile is not None and profile.egress_session
            else secrets.token_hex(6)
        )
        owned_cloud_id = cloud_stop_id
        remote = (cdp_url or "").strip()
        if not remote and browser_cloud.configured():
            cloud = browser_cloud.create_session()
            remote = cloud.cdp_url
            owned_cloud_id = cloud.id
            log.step(f"browser-use session {cloud.id}")
        if not remote:
            remote = settings.apply_browser_cdp_url.strip()
        if remote and profile is not None:
            log.step("local profile ignored: managed browser owns the fingerprint")
            profile = None
        with sync_playwright() as playwright:
            if remote:
                # Managed Chrome (Browser Use Cloud, or a legacy scraping
                # browser). Drive the context it already has; do not overlay
                # stealth -- that is the tell against a fingerprint they built.
                browser = playwright.chromium.connect_over_cdp(
                    remote, timeout=CDP_CONNECT_TIMEOUT_MS
                )
            else:
                browser = playwright.chromium.launch(
                    headless=settings.apply_browser_headless,
                    proxy=_proxy_config(settings.apply_proxy_url, session_token=session_token),
                    args=["--disable-blink-features=AutomationControlled"],
                )
            try:
                if remote:
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    context.set_default_timeout(ACTION_TIMEOUT_MS)
                    context.set_default_navigation_timeout(CDP_NAV_TIMEOUT_MS)
                    page = context.pages[0] if context.pages else context.new_page()
                    log.step("browser opened (managed chrome)")
                else:
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 900},
                        accept_downloads=False,
                        user_agent=(
                            (profile.user_agent if profile is not None else "")
                            or settings.apply_browser_user_agent
                            or None
                        ),
                        locale="en-US",
                        timezone_id="America/Los_Angeles",
                        storage_state=(
                            profile.storage_state
                            if profile is not None and profile.storage_state
                            else None
                        ),
                    )
                    context.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    )
                    context.set_default_timeout(ACTION_TIMEOUT_MS)
                    context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
                    page = context.new_page()
                    log.step(
                        "browser opened" + (" via proxy" if settings.apply_proxy_url else "")
                    )
                yield page
                if profile is not None:
                    with contextlib.suppress(Exception):
                        profile.saved_state = context.storage_state()
                        log.step("profile state captured")
            finally:
                with contextlib.suppress(Exception):
                    browser.close()
                if owned_cloud_id:
                    browser_cloud.stop_session(owned_cloud_id)
                    log.step(f"browser-use session {owned_cloud_id} stopped")
                log.step("browser closed")


def fill(page, specs: list[FieldSpec], *, log: RunLog) -> list[str]:
    """Fill what we can find. Returns the keys of required fields we could not.

    Never clicks a field it was not told about, and never types into one whose
    value is empty -- a blank we chose not to invent (``docgen/answers``) stays
    blank on the form too.
    """
    missing: list[str] = []
    for spec in specs:
        if not spec.value and spec.kind != "file":
            if spec.required:
                missing.append(spec.key)
            continue
        if spec.kind == "radio":
            # A radio group is many elements, so `_first` is the wrong shape:
            # the value picks which one, by each option's own label.
            if _check_radio(page, spec, log=log):
                log.filled[spec.key] = _recorded(spec)
            elif spec.required:
                missing.append(spec.key)
            continue
        located = _first(page, spec.selectors)
        if located is None:
            log.step(f"no field for {spec.key}")
            if spec.required:
                missing.append(spec.key)
            continue
        try:
            if spec.kind == "file":
                located.set_input_files(spec.value)
            elif spec.kind == "select":
                located.select_option(spec.value)
            elif spec.kind == "check":
                located.check()
            elif spec.kind == "password":
                # Workday's password box is a React component that ignores a
                # set value -- `fill` reported success while the field stayed
                # empty and Create Account submitted blank (live, Intapp
                # 2026-08-27). Type it as real keystrokes, then confirm the
                # value actually took (length only; the value is a secret).
                located.click()
                located.press_sequentially(spec.value, delay=25)
                got = (located.input_value() or "")
                if len(got) != len(spec.value):
                    raise RuntimeError(
                        f"password field did not accept the value ({len(got)}/{len(spec.value)} chars)"
                    )
            else:
                located.fill(spec.value)
        except Exception as exc:  # noqa: BLE001 - one field, not the run
            log.step(f"could not fill {spec.key}: {exc}")
            if spec.required:
                missing.append(spec.key)
            continue
        log.filled[spec.key] = _recorded(spec)
        log.step(f"filled {spec.key}")
    log.missing = missing
    return missing


#: How each radio option's label is read: its wrapping <label>, or the one its
#: id points at. Evaluated per option because the group shares one selector.
_RADIO_LABEL_JS = (
    "el => (el.closest('label')?.innerText"
    " || (el.id && document.querySelector(`label[for=\"${CSS.escape(el.id)}\"]`)?.innerText)"
    " || '').trim()"
)


def _check_radio(page, spec: FieldSpec, *, log: RunLog) -> bool:
    """Pick the option whose label matches the answer. Exact beats substring:
    'Yes' must not select 'Yes, with conditions' when a plain 'Yes' exists."""
    wanted = spec.value.strip().lower()
    try:
        group = page.locator(spec.selectors[0])
        count = group.count()
        exact = substring = None
        for index in range(count):
            option = group.nth(index)
            label = str(option.evaluate(_RADIO_LABEL_JS) or "").strip().lower()
            if not label:
                continue
            if label == wanted:
                exact = option
                break
            if substring is None and (wanted in label or label in wanted):
                substring = option
        chosen = exact or substring
        if chosen is None:
            log.step(f"no radio option matching {_recorded(spec)!r} for {spec.key}")
            return False
        chosen.check()
        log.step(f"checked {spec.key} = {_recorded(spec)}")
        return True
    except Exception as exc:  # noqa: BLE001 - one field, not the run
        log.step(f"could not pick a radio for {spec.key}: {exc}")
        return False


def first_match(page, selectors: tuple[str, ...]) -> str:
    """The first selector that resolves to something present, or empty.

    Public because both the adapters and ``ats-probe`` ask the same question --
    "is this form actually here?" -- and they must ask it the same way.
    """
    for selector in selectors:
        try:
            if page.locator(selector).first.count():
                return selector
        except Exception:  # noqa: BLE001 - a selector the engine dislikes is a miss
            continue
    return ""


def _first(page, selectors: tuple[str, ...]):
    """The first VISIBLE match across ``selectors``, or None.

    Scans every node a selector matches, not just ``.first``: a text selector
    like ``button:has-text("Sign In")`` matches the header's Sign In link, the
    Google/LinkedIn SSO buttons, and the dialog's submit all at once (live,
    Intapp 2026-08-27 — four nodes), and ``.first`` alone pins whichever is
    earliest in the DOM even when it is hidden or the wrong control.
    """
    for selector in selectors:
        try:
            located = page.locator(selector)
            for index in range(min(located.count(), 10)):
                node = located.nth(index)
                if node.is_visible():
                    return node
        except Exception:  # noqa: BLE001 - a bad selector is not a failure
            continue
    return None


def wait_for_any(page, selectors: tuple[str, ...], *, timeout_ms: int = 12000) -> str:
    """Wait until one of ``selectors`` exists, and return the one that hit.

    Opening a form is a navigation on some boards and a client-side route on
    others. A fixed sleep has to be long enough for the slowest of those, which
    makes it a tax on every fast one -- and when it is still too short the
    symptom is a field map that reads as entirely dead, which is the one
    failure this tier must not report wrongly.
    """
    waited = 0
    poll = 250
    while True:
        hit = first_match(page, selectors)
        if hit or waited >= timeout_ms:
            return hit
        page.wait_for_timeout(poll)
        waited += poll


def wait_for_visible(page, selectors: tuple[str, ...], *, timeout_ms: int = 12000) -> bool:
    """Wait until one of ``selectors`` is actually VISIBLE, not merely present.

    ``wait_for_any`` answers "is it in the DOM", which is the wrong question
    right before a click or a fill: Workday renders the sign-on chooser's
    nodes before it paints them, so the settle returned on presence and the
    click 300ms later found nothing visible and reported the button missing
    (live, Intapp 2026-08-27). Anything that acts on a control it just waited
    for should wait with this.
    """
    waited = 0
    poll = 250
    while True:
        if _first(page, selectors) is not None:
            return True
        if waited >= timeout_ms:
            return False
        page.wait_for_timeout(poll)
        waited += poll


def reach_form(
    page,
    *,
    landmark: tuple[str, ...],
    open_form: tuple[str, ...],
    log: RunLog,
) -> str:
    """Get from a posting page to its application form. Returns the landmark hit.

    Three shapes, and the waits are what tell them apart:

    * the form is already on the page (Greenhouse) -- the first wait returns at
      once and nothing is clicked;
    * the form is behind a control that is in the initial HTML (Lever);
    * the control itself is React-rendered (Ashby), so it does not exist yet
      when ``domcontentloaded`` fires. Clicking straight away finds nothing to
      click, and the whole field map then reads as dead -- which is exactly how
      a working Ashby board probed as unusable.

    Opening the form is a navigation, never a send.
    """
    hit = wait_for_any(page, landmark, timeout_ms=4000)
    if hit or not open_form:
        return hit
    if wait_for_any(page, open_form, timeout_ms=8000):
        click(page, open_form, log=log, what="open form")
    return wait_for_any(page, landmark)


def click(
    page, selectors: tuple[str, ...], *, log: RunLog, what: str, wait_ms: int = 0
) -> bool:
    """Click the first visible match. ``wait_ms`` bounds a poll for it to
    appear first — for a control the caller knows is coming (a dialog that is
    still painting), where an instant miss is a race, not an answer."""
    if wait_ms > 0:
        wait_for_visible(page, selectors, timeout_ms=wait_ms)
    located = _first(page, selectors)
    if located is None:
        log.step(f"no {what} to click")
        return False
    try:
        located.click()
    except Exception as exc:  # noqa: BLE001
        # Workday (and other overlay-heavy ATSes) float an invisible layer
        # over their buttons -- a `click_filter` div `intercepts pointer
        # events` and a normal click times out, so Sign In never submitted
        # and the run stalled on the login page (live, 2026-08-27). A forced
        # click dispatches to the real target under the overlay.
        log.step(f"click {what} intercepted, forcing: {str(exc)[:80]}")
        try:
            located.click(force=True, timeout=8000)
        except Exception as exc2:  # noqa: BLE001
            log.step(f"could not click {what}: {exc2}")
            return False
    log.step(f"clicked {what}")
    return True


#: Text that means the application went through, lowercased. Generic across
#: vendors on purpose: every board says some version of thank-you.
_SUCCESS_MARKERS = (
    "thank you for applying",
    "application submitted",
    "application received",
    "successfully submitted",
    "we've received your application",
    "we have received your application",
    "your application has been submitted",
)


def _success_visible(page) -> bool:
    try:
        text = (page.inner_text("body") or "").lower()
    except Exception:  # noqa: BLE001 - a mid-navigation read is not evidence
        return False
    return any(marker in text for marker in _SUCCESS_MARKERS)


def confirm_submission(
    page,
    *,
    landmark: tuple[str, ...],
    submit: tuple[str, ...],
    log: RunLog,
    timeout_ms: int = 20000,
) -> bool:
    """Positive evidence the form was accepted, or False.

    "We clicked" is not "they accepted". The first live run proved it: the
    click landed 300ms after the resume was attached, the upload was still in
    flight, Ashby swallowed the click, and the page at +3s was the same filled
    form -- which the old code recorded as a successful submission.

    Evidence is either a thank-you message or the form itself going away. If
    the form is still standing halfway through the window, the submit is
    clicked once more: a click a vendor ignored mid-upload and a click that
    worked leave distinguishable pages, so the retry cannot double-submit --
    a form that already went is not there to click.
    """
    waited = 0
    poll = 500
    retried = False
    while waited < timeout_ms:
        if _success_visible(page):
            log.step("confirmation text visible")
            return True
        if not first_match(page, landmark):
            log.step("form gone after submit")
            return True
        if not retried and waited >= timeout_ms // 2:
            # Still standing at half time: the first click most likely landed
            # during the resume upload. One more, never a third.
            retried = True
            log.step("form still present; clicking submit once more")
            click(page, submit, log=log, what="submit (retry)")
        page.wait_for_timeout(poll)
        waited += poll
    log.step("no confirmation within the window")
    return False


def snapshot(page, *, org_id: str, application_id: str, label: str, log: RunLog) -> str:
    """Screenshot into the document store; returns its storage-relative path."""
    relpath = storage.artifact_relpath(
        org_id=org_id, application_id=application_id, label=label, suffix=".png"
    )
    try:
        # Screenshots are durable artifacts.  Password controls normally draw
        # bullets, but a board can style them unusually; hide every password
        # control before capture without changing its value or form state.
        page.evaluate(
            """() => {
              let style = document.getElementById('__juicebox-secret-scrub');
              if (!style) {
                style = document.createElement('style');
                style.id = '__juicebox-secret-scrub';
                style.textContent = 'input[type="password"] { color: transparent !important; text-shadow: none !important; caret-color: transparent !important; }';
                document.head.appendChild(style);
              }
            }"""
        )
        image = page.screenshot(full_page=True)
    except Exception as exc:  # noqa: BLE001 - never fail a run over evidence
        log.step(f"could not screenshot at {label}: {exc}")
        return ""
    storage.save_bytes(relpath, image)
    log.artifacts.append(relpath)
    log.step(f"captured {label}")
    return relpath


def open_verification_link(url: str) -> bool:
    """Click through an employer's email-verification link.

    A plain GET is often enough, but a portal that sets a session cookie on the
    click needs a real browser, and the difference is invisible until an account
    silently stays unverified. So: browser, one page, no form.
    """
    log = RunLog()
    try:
        with session(log=log) as page:
            page.goto(url, wait_until="domcontentloaded")
            return True
    except BrowserUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("verification link %s did not open: %s", url, exc)
        return False
