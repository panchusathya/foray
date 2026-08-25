"""Only an explicit "gone" counts as gone.

A live thread was sent a Rippling role whose page had already been taken
down: two days old and perfectly fresh by the "recently seen" measure, which
is a different question from whether it still exists.

The asymmetry here is the whole design. A dead link is an annoyance the
candidate can see and forgive. Deleting a live role because a job board
answered 403 to a datacentre IP is invisible -- to them and to us -- and
silently shrinks somebody's feed.
"""

from __future__ import annotations

import httpx
import pytest

from juicebox.jobmatch.liveness import is_gone


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _status(code: int):
    return lambda request: httpx.Response(code)


class TestIsGone:
    @pytest.mark.parametrize("code", [404, 410])
    def test_explicitly_gone(self, code):
        with _client(_status(code)) as c:
            assert is_gone("https://jobs.example.com/1", client=c)

    @pytest.mark.parametrize("code", [200, 301, 302, 401, 403, 429, 500, 503])
    def test_everything_else_is_alive(self, code):
        """403 and 429 are boards blocking us, not roles disappearing."""
        with _client(_status(code)) as c:
            assert not is_gone("https://jobs.example.com/1", client=c)

    def test_a_network_error_is_not_death(self):
        def boom(request):
            raise httpx.ConnectError("dns", request=request)

        with _client(boom) as c:
            assert not is_gone("https://jobs.example.com/1", client=c)

    def test_head_not_allowed_falls_back_to_get(self):
        """Some boards answer 405 to HEAD. That is a verb, not a deletion."""
        seen = []

        def handler(request):
            seen.append(request.method)
            if request.method == "HEAD":
                return httpx.Response(405)
            return httpx.Response(404)

        with _client(handler) as c:
            assert is_gone("https://jobs.example.com/1", client=c)
        assert seen == ["HEAD", "GET"]

    def test_a_missing_or_odd_url_is_never_gone(self):
        with _client(_status(404)) as c:
            assert not is_gone("", client=c)
            assert not is_gone("mailto:someone@example.com", client=c)
            assert not is_gone("/relative/path", client=c)
