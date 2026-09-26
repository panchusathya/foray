"""The sender that can carry the alias.

Everything here is about the request body, because the two reasons this
provider exists are both things the *other* providers dropped on the floor
rather than refused loudly:

* a sub-addressed ``From`` -- Zoho answers 500 "Given FromAddress not exists!"
* ``Reply-To`` -- Zoho answers 500 "You need to verify the ReplyTo address"

So the tests assert what goes on the wire, not that a mock was called.
"""

from __future__ import annotations

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

    def test_attachments_travel_as_base64(self):
        from juicebox.applications.submit.base import Attachment

        client = FakeClient()
        result = ResendSender("re_key", client=client).send_with_attachments(
            _email(),
            [Attachment("resume.pdf", "application/pdf", b"%PDF-1.4")],
        )
        assert result.ok is True
        attached = client.calls[0]["json"]["attachments"]
        assert attached[0]["filename"] == "resume.pdf"
        assert attached[0]["content_type"] == "application/pdf"
        import base64

        assert base64.b64decode(attached[0]["content"]) == b"%PDF-1.4"


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

    def test_a_transport_failure_is_not_an_exception(self):
        result, _ = _send(_email(), raises=RuntimeError("connection reset"))
        assert result.ok is False
        assert "connection reset" in result.error

    def test_no_key_refuses_before_the_network(self):
        client = FakeClient()
        result = ResendSender("", client=client).send(_email())
        assert result.ok is False
        assert "RESEND_API_KEY" in result.error
        assert client.calls == [], "it must not reach the network without a key"
