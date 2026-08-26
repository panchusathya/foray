"""Candidate-side app connectors (Gmail, Outlook, GitHub).

Deliberately a sibling of ``integrations/`` rather than an extension of it.
That package holds *recruiter* mailboxes: org-scoped, send-capable, synced on
a loop. These are *candidate* grants: thread-scoped, read-only, indexed once
at connect time. The trust model, the scopes, and the data-minimisation rules
are different enough that sharing a registry would invite exactly the mixups
the split prevents — a candidate token must never be able to send mail.
"""

from .base import (
    CandidateConnector,
    ConnectorError,
    ConnectorGrant,
    ConnectorProfile,
    ResumeHit,
    configured_connectors,
    connector_names,
    get_connector,
    reset_connectors,
    set_connector,
)

__all__ = [
    "CandidateConnector",
    "ConnectorError",
    "ConnectorGrant",
    "ConnectorProfile",
    "ResumeHit",
    "configured_connectors",
    "connector_names",
    "get_connector",
    "reset_connectors",
    "set_connector",
]
