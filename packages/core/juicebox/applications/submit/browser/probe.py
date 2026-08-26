"""Point a browser at a real posting and report which selectors still resolve.

The failure mode of the browser tier is not a bug in our code -- it is a vendor
reshaping their form. This is the tool that finds out, before a candidate's
application does. It reads the same ``FormSpec`` the adapter fills, so a probe
run validates the exact map that will be used.

**It never fills and never submits.** It navigates, optionally clicks the
control that opens the form (which is a page transition, not an application),
counts what it can find, and screenshots. Running it against somebody's live
posting costs them one page view.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ...ats.detect import detect_target
from . import driver
from .driver import RunLog
from .forms import PROBEABLE, FormSpec

logger = logging.getLogger(__name__)


@dataclass
class FieldReport:
    key: str
    required: bool
    #: the first selector that resolved, or empty
    matched: str = ""
    tried: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.matched)


@dataclass
class ProbeReport:
    url: str
    vendor: str = ""
    reached_form: bool = False
    fields: list[FieldReport] = field(default_factory=list)
    submit: FieldReport | None = None
    screenshot: str = ""
    error: str = ""

    @property
    def missing_required(self) -> list[str]:
        return [row.key for row in self.fields if row.required and not row.ok]

    @property
    def usable(self) -> bool:
        """Whether an adapter run would get as far as pressing submit."""
        return (
            self.reached_form
            and not self.missing_required
            and self.submit is not None
            and self.submit.ok
        )

    def render(self) -> str:
        lines = [f"{self.url}", f"  vendor: {self.vendor or '(unrecognised)'}"]
        if self.error:
            lines.append(f"  error: {self.error}")
            return "\n".join(lines)
        lines.append(f"  form reached: {'yes' if self.reached_form else 'NO'}")
        for row in self.fields:
            mark = "ok " if row.ok else ("MISS" if row.required else "-   ")
            detail = row.matched or f"none of {len(row.tried)} selectors"
            lines.append(f"  {mark} {row.key:16} {detail}")
        if self.submit is not None:
            mark = "ok " if self.submit.ok else "MISS"
            lines.append(f"  {mark} {'submit':16} {self.submit.matched or 'not found'}")
        if self.screenshot:
            lines.append(f"  screenshot: {self.screenshot}")
        lines.append(f"  VERDICT: {'usable' if self.usable else 'NOT usable as mapped'}")
        return "\n".join(lines)


def _find(page, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        try:
            located = page.locator(selector).first
            if located.count():
                return selector
        except Exception:  # noqa: BLE001 - a selector the engine dislikes is a miss
            continue
    return ""


def probe(url: str, *, org_id: str = "probe", spec: FormSpec | None = None) -> ProbeReport:
    """Navigate one posting and report on its form. Read-only."""
    target = detect_target(url)
    spec = spec or PROBEABLE.get(target.vendor)
    report = ProbeReport(url=url, vendor=target.vendor)
    if spec is None:
        report.error = f"no field map for {target.vendor or 'this URL'}"
        return report

    log = RunLog()
    try:
        with driver.session(log=log) as page:
            page.goto(url, wait_until="domcontentloaded")
            # Opening the form is a navigation, not an application.
            report.reached_form = bool(
                driver.reach_form(page, landmark=spec.landmark, open_form=spec.open_form, log=log)
            )

            for key, selectors in spec.fields.items():
                report.fields.append(
                    FieldReport(
                        key=key,
                        required=key in spec.required,
                        matched=_find(page, selectors),
                        tried=selectors,
                    )
                )
            report.submit = FieldReport(
                key="submit", required=True, matched=_find(page, spec.submit), tried=spec.submit
            )
            report.screenshot = driver.snapshot(
                page,
                org_id=org_id,
                application_id=f"probe-{spec.vendor}",
                label="probe",
                log=log,
            )
    except driver.BrowserUnavailable as exc:
        report.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - a probe never raises at the caller
        report.error = f"{type(exc).__name__}: {exc}"
    return report
