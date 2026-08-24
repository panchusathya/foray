"""Candidate documents: parse what they upload, generate what they need.

The house posture throughout (CANDIDATE_AGENT_PLAN §7-§8):

* **The deterministic path is complete.** Every builder here produces a full,
  correct document from the Candidate/Experience/Education rows and the intake
  preference with no API key at all; the model's whole job is selection,
  ordering, and tightening.
* **The model never invents.** Not an employer, not a date, not a credential.
  The tailor prompt says so, and the tests hand it a transport that tries.
* **Parsed facts are not applied facts.** A parsed upload lands on
  ``CandidateDocument.structured`` and is promoted to the Candidate by an
  explicit merge step -- never by the blind upsert that rebuilds experience
  rows and feeds the search haystack.
"""
