"""The sender that can carry the alias.

Everything here is about the request body, because the two reasons this
provider exists are both things the *other* providers dropped on the floor
rather than refused loudly:

* a sub-addressed ``From`` -- Zoho answers 500 "Given FromAddress not exists!"
* ``Reply-To`` -- Zoho answers 500 "You need to verify the ReplyTo address"

So the tests assert what goes on the wire, not that a mock was called.
"""

from __future__ import annotations

import pytest

from juicebox.outreach.sender import OutboundEmail, ResendSender

_UNSET = object()


class FakeResponse:
    """`payload=None` has to mean "body is not JSON", so the default is a
    sentinel rather than None -- otherwise the two cases collide."""

    def __init__(self, status_code=200, payload=_UNSET, text=""):
        self.status_code = status_code
        self._payload = {"id": "re_abc123"} if payload is _UNSET else payload
        self.text = text or "{}"

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    def __init__(self, response=None, raises=None):
        self.response = response or FakeResponse()
        self.raises = raises
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.raises:
            raise self.raises
        return self.response

    def close(self):  # pragma: no cover - the sender only closes what it made
        raise AssertionError("an injected client must not be closed by the sender")


def _email(**kwargs) -> OutboundEmail:
    base = dict(
        to="candidate@example.com",
        subject="Applied: Analyst at Acme",
        text_body="body",
        html_body="",
        from_email="anna+c0ffee@goforay.io",
        from_name="Foray",
    )
    base.update(kwargs)
    return OutboundEmail(**base)


def _send(email, response=None, raises=None):
    client = FakeClient(response=response, raises=raises)
    result = ResendSender("re_key", client=client).send(email)
    return result, (client.calls[0] if client.calls else None)


class TestTheWireFormat:
    def test_a_sub_addressed_from_survives(self):
        """The whole point. Zoho flattens this one to the bare mailbox."""
        _, call = _send(_email())
        assert call["json"]["from"] == "Foray <anna+c0ffee@goforay.io>"

    def test_reply_to_is_a_field_not_a_header(self):
        """Sending it in both places puts two Reply-To lines on the wire."""
        _, call = _send(_email(headers={"Reply-To": "anna+c0ffee@goforay.io"}))
        assert call["json"]["reply_to"] == "anna+c0ffee@goforay.io"
        assert "Reply-To" not in call["json"].get("headers", {})

    def test_threading_hints_travel_as_headers(self):
        _, call = _send(_email(in_reply_to="<abc@ashbyhq.com>", references="<abc@ashbyhq.com>"))
        assert call["json"]["headers"]["In-Reply-To"] == "<abc@ashbyhq.com>"
        assert call["json"]["headers"]["References"] == "<abc@ashbyhq.com>"

    def test_an_explicit_header_wins_over_the_field(self):
        _, call = _send(_email(in_reply_to="<derived>", headers={"In-Reply-To": "<explicit>"}))
        assert call["json"]["headers"]["In-Reply-To"] == "<explicit>"

    def test_the_recipient_is_a_list(self):
        _, call = _send(_email())
        assert call["json"]["to"] == ["candidate@example.com"]

    def test_html_is_omitted_when_empty(self):
        """An empty html part renders as a blank message in some clients."""
        _, call = _send(_email())
        assert "html" not in call["json"]

    def test_html_is_sent_when_present(self):
        _, call = _send(_email(html_body="<p>hi</p>"))
        assert call["json"]["html"] == "<p>hi</p>"

    def test_the_key_is_a_bearer_token(self):
        _, call = _send(_email())
        assert call["headers"]["Authorization"] == "Bearer re_key"


class TestResults:
    def test_a_send_reports_the_provider_id(self):
        result, _ = _send(_email())
        assert result.ok is True
        assert result.provider_native_id == "re_abc123"

    def test_no_rfc_message_id_is_claimed(self):
        """Resend does not return one, and inventing one would break threading
        for anything that later tried to match on it."""
        result, _ = _send(_email())
        assert result.internet_message_id == ""

    def test_a_rejection_keeps_the_reason(self):
        """"The mail did not arrive" is not a debuggable error; the body says
        whether the domain is unverified or the From is not ours."""
        response = FakeResponse(status_code=403, text='{"message":"domain not verified"}')
        result, _ = _send(_email(), response=response)
        assert result.ok is False
        assert "403" in result.error
        assert "domain not verified" in result.error

    def test_a_transport_failure_is_not_an_exception(self):
        result, _ = _send(_email(), raises=RuntimeError("connection reset"))
        assert result.ok is False
        assert "connection reset" in result.error

    def test_a_success_with_an_unreadable_body_still_succeeds(self):
        result, _ = _send(_email(), response=FakeResponse(payload=None))
        assert result.ok is True
        assert result.provider_message_id == "resend"

    def test_no_key_refuses_before_the_network(self):
        client = FakeClient()
        result = ResendSender("", client=client).send(_email())
        assert result.ok is False
        assert "RESEND_API_KEY" in result.error
        assert client.calls == [], "it must not reach the network without a key"


class TestSelection:
    def test_get_sender_builds_it_from_the_provider_setting(self, monkeypatch):
        from juicebox.config import reset_settings_cache
        from juicebox.outreach import sender as sender_mod

        monkeypatch.setenv("EMAIL_PROVIDER", "resend")
        monkeypatch.setenv("RESEND_API_KEY", "re_from_env")
        reset_settings_cache()
        sender_mod.set_sender(None)
        try:
            built = sender_mod.get_sender()
            assert isinstance(built, ResendSender)
            assert built.api_key == "re_from_env"
        finally:
            sender_mod.set_sender(None)
            reset_settings_cache()

    @pytest.mark.parametrize("provider", ["console", "memory"])
    def test_other_providers_are_untouched(self, monkeypatch, provider):
        from juicebox.config import reset_settings_cache
        from juicebox.outreach import sender as sender_mod

        monkeypatch.setenv("EMAIL_PROVIDER", provider)
        reset_settings_cache()
        sender_mod.set_sender(None)
        try:
            assert sender_mod.get_sender().name == provider
        finally:
            sender_mod.set_sender(None)
            reset_settings_cache()
