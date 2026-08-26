"""Signed public URL for a job-match vCard.

Linq fetches a public HTTPS URL for a media part; the vCard therefore has to
be served from our API the same way a resume-upload link is. Non-expiring by
design: a card Linq (or the candidate) opens a day later still has to render,
and the proposal's own lifecycle is what ends its usefulness.
"""

from __future__ import annotations

from ..config import get_settings
from ..models import JobProposal
from ..security import signed_intake_token, verify_intake_token

PURPOSE_JOBCARD = "intake-jobcard"


def mint_jobcard_link(proposal: JobProposal) -> str:
    """The absolute URL Linq fetches as the contact-card media part."""
    base = get_settings().base_url.rstrip("/")
    token = signed_intake_token(PURPOSE_JOBCARD, proposal.id)
    return f"{base}/v1/intake/jobcards/{proposal.id}.vcf?token={token}"


def verify_jobcard_token(proposal_id: str, token: str) -> bool:
    return verify_intake_token(PURPOSE_JOBCARD, proposal_id, token)
