"""Execute a policy-approved action through Playwright."""

from __future__ import annotations

from .actions import Action
from .observe import locator
from .policy import Decision


def execute(page, action: Action, decision: Decision) -> str:
    """Run one approved action. Returns a short executed-action label."""
    if action.type == "stop":
        return "stop"
    if action.type == "fill":
        locator(page, action.control_id).fill(decision.resolved_value)
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
