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

logger = logging.getLogger(__name__)

# Serialises Chromium across worker threads, and paces us at a rate a careers
# portal would not notice. Same reasoning as linkedin_scrape._RATE_LOCK.
_RATE_LOCK = threading.Lock()
_LAST_RUN_AT = 0.0
_MIN_INTERVAL_SECONDS = 4.0

NAV_TIMEOUT_MS = 45_000
ACTION_TIMEOUT_MS = 15_000


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
    kind: str = "text"  # text | file | select | check


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


def available() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
    except ImportError as exc:
        return False, f"playwright is not installed ({exc})"
    return True, ""


@contextlib.contextmanager
def session(*, log: RunLog):
    """A paced, serialised browser context. Closes whatever it opened."""
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
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=settings.apply_browser_headless)
            try:
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    accept_downloads=False,
                )
                context.set_default_timeout(ACTION_TIMEOUT_MS)
                context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
                page = context.new_page()
                log.step("browser opened")
                yield page
            finally:
                with contextlib.suppress(Exception):
                    browser.close()
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
            else:
                located.fill(spec.value)
        except Exception as exc:  # noqa: BLE001 - one field, not the run
            log.step(f"could not fill {spec.key}: {exc}")
            if spec.required:
                missing.append(spec.key)
            continue
        log.filled[spec.key] = spec.value if spec.kind != "file" else "<file>"
        log.step(f"filled {spec.key}")
    log.missing = missing
    return missing


def _first(page, selectors: tuple[str, ...]):
    for selector in selectors:
        try:
            located = page.locator(selector).first
            if located.count() and located.is_visible():
                return located
        except Exception:  # noqa: BLE001 - a bad selector is not a failure
            continue
    return None


def click(page, selectors: tuple[str, ...], *, log: RunLog, what: str) -> bool:
    located = _first(page, selectors)
    if located is None:
        log.step(f"no {what} to click")
        return False
    try:
        located.click()
    except Exception as exc:  # noqa: BLE001
        log.step(f"could not click {what}: {exc}")
        return False
    log.step(f"clicked {what}")
    return True


def snapshot(page, *, org_id: str, application_id: str, label: str, log: RunLog) -> str:
    """Screenshot into the document store; returns its storage-relative path."""
    relpath = storage.artifact_relpath(
        org_id=org_id, application_id=application_id, label=label, suffix=".png"
    )
    try:
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
