"""Sending applications: the seam, the adapters, and the registry.

``base`` is the contract, ``registry`` picks the adapter for a target, and the
rest are one vendor each. Nothing here decides whether an application is
*allowed* to be sent -- that lives in ``applications.submission`` and
``applications.state``.
"""

from .base import (
    METHOD_ATS_API,
    METHOD_BROWSER,
    METHOD_CONSOLE,
    METHOD_EMAIL,
    METHOD_MEMORY,
    Attachment,
    SubmissionContext,
    SubmissionResult,
    SubmitError,
    Submitter,
)
from .registry import get_submitter, memory_submitter, reset_overrides, set_submitter, submittable

__all__ = [
    "METHOD_ATS_API",
    "METHOD_BROWSER",
    "METHOD_CONSOLE",
    "METHOD_EMAIL",
    "METHOD_MEMORY",
    "Attachment",
    "SubmissionContext",
    "SubmissionResult",
    "SubmitError",
    "Submitter",
    "get_submitter",
    "memory_submitter",
    "reset_overrides",
    "set_submitter",
    "submittable",
]
