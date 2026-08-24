"""The candidate portal's server side: link minting, auth, and its API.

apply.goforay.io is the visual surface of the messages-first agent — the
swipe deck, the tracker, the documents. Everything here is scoped to exactly
one candidate by ``CandidatePrincipal``, and every serializer is a whitelist:
recruiter-internal fields (scores, criteria, crm_status, notes,
confidential_items) never cross this boundary, and the leak tests are the
contract.
"""
