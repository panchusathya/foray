"""Decline-to-self-identify, not an assertion of a protected attribute.

Voluntary EEO / demographic questions are answered with the form's own
"Prefer not to answer" option when it has one. That is a refusal to state
a protected attribute, which is the opposite of inventing one. A required
self-ID control with no decline option is still a hand-off.
"""

from __future__ import annotations

import re

_DECLINE = re.compile(
    r"prefer not to (answer|say|identify|disclose|self[- ]?identify)"
    r"|decline to (self[- ]?identify|answer|disclose|identify)"
    r"|do not wish to (answer|identify|disclose|self[- ]?identify)"
    r"|don't wish to (answer|identify|disclose)"
    r"|choose not to (answer|disclose|identify|self[- ]?identify)"
    r"|i prefer not to"
    r"|opt out",
    re.I,
)


def is_decline(text: str) -> bool:
    return bool(_DECLINE.search((text or "").strip()))


def decline_choice(options: list[str] | tuple[str, ...] | None) -> str:
    """The form's own decline option, or empty when it does not offer one."""
    for option in options or ():
        if is_decline(str(option)):
            return str(option)
    return ""
