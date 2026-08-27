"""Governed browser-agent submitter: typed actions, policy, Juicebox planner.

A model proposes one next action; policy decides; Playwright executes on a
Browser Use Cloud Chrome. The hosted Browser Use agent product (``POST /runs``)
is out of scope — Juicebox keeps the wheel.
"""

from .preflight import discover_questions, merge_discovered
from .submitter import BrowserAgentSubmitter

__all__ = ["BrowserAgentSubmitter", "discover_questions", "merge_discovered"]
