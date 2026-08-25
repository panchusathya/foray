"""Is this posting still there?

Freshness in `bar.py` answers "did we see it recently", which is a different
question from "does it still exist". A live thread was sent a Rippling role
whose page had already been taken down -- the posting was two days old and
perfectly fresh by that measure.

Checked at proposal time rather than in a sweep, because that is the moment
the answer matters and the moment the set is small: at most three URLs, right
before we put them in front of somebody.

**Only an explicit "gone" counts.** Job boards routinely answer 403 to a bare
HEAD from a datacentre IP, redirect to a login wall, or rate-limit. Treating
any of that as death would quietly delete good roles from somebody's feed --
a far worse failure than the occasional dead link, because it is invisible.
So: 404 and 410 are dead, everything else is alive, and a network error is
alive too.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# Deliberately short. This runs inline while somebody waits for job cards, and
# a slow board must cost a second, not a conversation.
TIMEOUT_SECONDS = 4.0
GONE_STATUSES = frozenset({404, 410})

_HEADERS = {
    # A bare client gets blocked by boards that are perfectly happy to serve a
    # browser. We are reading a public posting we were pointed at, not
    # crawling, and being blocked would read to us as "role deleted".
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def is_gone(url: str, *, client: httpx.Client | None = None) -> bool:
    """True only when the page is explicitly gone. Never guesses."""
    target = (url or "").strip()
    if not target.startswith(("http://", "https://")):
        return False

    owns = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT_SECONDS, follow_redirects=True, headers=_HEADERS
    )
    try:
        try:
            response = client.head(target)
            # Plenty of boards do not implement HEAD and answer 405; ask again
            # properly rather than record a role as dead over a verb.
            if response.status_code in {405, 501}:
                response = client.get(target)
        except httpx.HTTPError as exc:
            logger.debug("liveness check could not reach %s: %s", target, exc)
            return False
        gone = response.status_code in GONE_STATUSES
        if gone:
            logger.info("posting is gone (%s): %s", response.status_code, target)
        return gone
    finally:
        if owns:
            client.close()
