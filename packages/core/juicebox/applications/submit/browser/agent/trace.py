"""Redacted step trace for a browser-agent run."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ...base import REDACTED
from .actions import Action
from .policy import Decision


def utcstamp() -> str:
    return datetime.now(UTC).isoformat()


def record(
    *,
    observation_fingerprint: str,
    url: str,
    page_class: str,
    proposed: Action | None,
    decision: Decision | None,
    executed: str,
    at: str = "",
    control_label: str = "",
    control_count: int = -1,
) -> dict[str, Any]:
    action = None
    if proposed is not None:
        action = {
            "type": proposed.type,
            "control_id": proposed.control_id,
            "value_token": proposed.value_token,
        }
        if control_label:
            # WHAT the id pointed at. Reading a trace of bare c-numbers, the
            # step where the planner clicked "Back to Job Posting" was
            # indistinguishable from a wizard click (live, Intapp 2026-08-27).
            action["control_label"] = control_label[:120]
    return {
        "at": at or utcstamp(),
        "url": _redact_query(url),
        "page_class": page_class,
        "observation": observation_fingerprint,
        "controls": control_count,
        "proposed": action,
        "policy": {
            "allowed": bool(decision and decision.allowed),
            "reason": (decision.reason if decision else "")[:300],
        },
        "executed": executed,
    }


def payload(
    *,
    planner: str = "browser_agent",
    session_id: str = "",
    final_submit_requested_at: str = "",
    steps: list[dict],
    artifacts: list[str],
) -> dict[str, Any]:
    return {
        "planner": planner,
        "provider_session_id": session_id,
        "final_submit_requested_at": final_submit_requested_at,
        "steps": steps,
        "filled": REDACTED,
        "artifacts": list(artifacts),
    }


def _redact_query(url: str) -> str:
    if "?" not in url:
        return url
    return url.split("?", 1)[0] + "?redacted"
