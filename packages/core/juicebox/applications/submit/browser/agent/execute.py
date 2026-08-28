"""Execute a policy-approved action through Playwright."""

from __future__ import annotations

from .actions import Action
from .observe import locator
from .policy import Decision

#: Is a type-ahead suggestion list open right now? Workday's multiselects
#: pop an activeListContainer listbox while you type; Enter then commits the
#: typed text as a selection pill (verified live, Intapp 2026-08-27).
_LISTBOX_OPEN_JS = """() => {
  const el = document.querySelector(
    '[data-automation-id="activeListContainer"], [role="listbox"]'
  );
  if (!el) return false;
  const s = window.getComputedStyle(el);
  const b = el.getBoundingClientRect();
  return s.display !== 'none' && s.visibility !== 'hidden' && b.width > 0 && b.height > 0;
}"""


def execute(page, action: Action, decision: Decision) -> str:
    """Run one approved action. Returns a short executed-action label."""
    if action.type == "stop":
        return "stop"
    if action.type == "fill":
        loc = locator(page, action.control_id)
        # Real keystrokes, not a programmatic set: Workday's multiselect
        # search boxes open their suggestion popup on key events, so .fill()
        # typed the text, no popup ever opened, the value never committed and
        # the planner re-filled the same box to the fault budget (live,
        # Intapp 2026-08-27) -- the same lesson the password field taught.
        # Plain inputs type fine either way; the popup then gets a beat to
        # open so the NEXT observation can see its option rows.
        try:
            loc.click()
            loc.fill("")
            loc.press_sequentially(decision.resolved_value, delay=25)
            # The suggestion list is a server round-trip (~2.5s measured);
            # 1200ms observed an unopened popup and the value never committed
            # (live, Intapp 2026-08-27).
            page.wait_for_timeout(2500)
            if page.evaluate(_LISTBOX_OPEN_JS):
                # Enter commits the typed text as a pill when it names a real
                # option; a plain input never has a listbox open, so this
                # never submits a form.
                loc.press("Enter")
                page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001 - a widget that refuses keystrokes gets the plain set
            loc.fill(decision.resolved_value)
        return f"fill:{action.control_id}"
    if action.type == "select":
        loc = locator(page, action.control_id)
        try:
            loc.select_option(decision.resolved_value)
        except Exception:  # noqa: BLE001 - label match is the usual fallback
            loc.select_option(label=decision.resolved_value)
        return f"select:{action.control_id}"
    if action.type == "upload":
        locator(page, action.control_id).set_input_files(decision.resolved_file)
        return f"upload:{action.control_id}"
    if action.type == "check":
        loc = locator(page, action.control_id)
        loc.check()
        return f"check:{action.control_id}"
    if action.type in {"click_next", "dismiss", "final_submit"}:
        # Policy may have resolved the real control itself — final_submit
        # never clicks the model's id (it invented ">FINAL" live while the
        # true send button sat in the inventory).
        cid = decision.resolved_control_id or action.control_id
        if cid:
            locator(page, cid).click()
        return f"{action.type}:{cid or 'default'}"
    return action.type
