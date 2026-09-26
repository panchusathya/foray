"""Sending app-level mail through a connected mailbox instead of SMTP.

The reason this mode exists is infrastructural, not architectural: the host
this runs on silently drops outbound 25/465/587/2525, so `EMAIL_PROVIDER=smtp`
cannot deliver from there however correct the credentials are, while the
provider APIs on 443 work fine.

What these tests are really protecting is the *loudness* of the failure paths.
The OTP endpoint answers 202 whatever happens, so that it cannot be used to
enumerate addresses -- which means a misconfigured mailbox has no user-visible
symptom at all beyond "the code never arrived". Every refusal below therefore
has to be a FailingSender that logs, never a silent fall back to console.
"""

from __future__ import annotations

import pytest

from juicebox.config import reset_settings_cache
from juicebox.models import MailboxConnection
from juicebox.outreach import sender as sender_mod


@pytest.fixture
def mailbox_mode(monkeypatch):
    """Turn on `EMAIL_PROVIDER=mailbox` pointing at anna@goforay.io."""

    def _apply(address: str = "anna@goforay.io"):
        monkeypatch.setenv("EMAIL_PROVIDER", "mailbox")
        monkeypatch.setenv("EMAIL_MAILBOX_ADDRESS", address)
        reset_settings_cache()

    yield _apply
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("EMAIL_MAILBOX_ADDRESS", raising=False)
    reset_settings_cache()


def _connection(session, account, **kwargs) -> MailboxConnection:
    row = MailboxConnection(
        org_id=account["org_id"],
        provider="zoho",
        address=kwargs.pop("address", "anna@goforay.io"),
        status=kwargs.pop("status", "active"),
        send_enabled=kwargs.pop("send_enabled", True),
        **kwargs,
    )
    session.add(row)
    session.flush()
    return row


class TestResolution:
    def test_refuses_when_the_mailbox_is_not_connected(self, session, account, mailbox_mode):
        mailbox_mode()
        resolved = sender_mod.sender_for_org(session, account["org_id"])
        assert isinstance(resolved, sender_mod.FailingSender)
        assert "not a connected mailbox" in resolved.reason

    def test_refuses_a_revoked_mailbox(self, session, account, mailbox_mode):
        _connection(session, account, status="revoked")
        mailbox_mode()
        resolved = sender_mod.sender_for_org(session, account["org_id"])
        assert isinstance(resolved, sender_mod.FailingSender)
        assert "revoked" in resolved.reason

    def test_refuses_when_sending_is_switched_off(self, session, account, mailbox_mode):
        _connection(session, account, send_enabled=False)
        mailbox_mode()
        resolved = sender_mod.sender_for_org(session, account["org_id"])
        assert isinstance(resolved, sender_mod.FailingSender)
        assert "switched off" in resolved.reason

    def test_does_not_reach_another_orgs_mailbox(self, session, account, mailbox_mode):
        """The address is matched within the sending org, never globally."""
        _connection(session, account)
        mailbox_mode()
        resolved = sender_mod.sender_for_org(session, "org_someone_else")
        assert isinstance(resolved, sender_mod.FailingSender)

    def test_ignores_the_daily_cap(self, session, account, mailbox_mode):
        """A sign-in code is not outreach.

        Refusing one because an outreach mailbox is at its campaign cap would
        lock a candidate out of their own account.
        """
        connection = _connection(session, account, daily_send_cap=0)
        mailbox_mode()
        sender_mod.set_sender_factory("zoho", lambda db, conn: sender_mod.MemorySender())
        try:
            resolved = sender_mod.sender_for_org(session, account["org_id"])
        finally:
            sender_mod.reset_sender_factories()
        assert isinstance(resolved, sender_mod.MemorySender)
        assert connection.daily_send_cap == 0


class TestGlobalSenderGuard:
    """`get_sender()` cannot build a mailbox sender, and must not pretend to.

    Falling through to console here is the dangerous default: the deployment
    is configured for real delivery, ConsoleSender reports every send as a
    success, and `EmailSubmitter` reads that success as "the employer has it".
    """

    def test_an_email_submitter_cannot_report_success(self, monkeypatch):
        """The specific regression: console returns ok=True having only logged,
        which marks an application submitted with nothing sent."""
        monkeypatch.setenv("EMAIL_PROVIDER", "mailbox")
        reset_settings_cache()
        sender_mod.set_sender(None)
        try:
            sender = sender_mod.get_sender()
            assert not hasattr(sender, "send_with_attachments")
            assert sender.send(_probe_email()).ok is False
        finally:
            sender_mod.set_sender(None)
            monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
            reset_settings_cache()

def _probe_email():
    from juicebox.outreach.sender import OutboundEmail

    return OutboundEmail(
        to="x@example.com",
        subject="s",
        text_body="b",
        html_body="",
        from_email="anna@goforay.io",
    )


class TestZohoFromAddress:
    """Zoho sends as the connected mailbox and nothing else."""

    def _sender(self, address: str = "anna@goforay.io"):
        from juicebox.integrations.zoho import ZohoSender

        class _FakeClient:
            def __init__(self):
                self.sent = []

            def send_message(self, body):
                self.sent.append(body)
                return {"messageId": "zm-1", "mailId": "<abc@zoho>"}

        connection = MailboxConnection(
            org_id="org_x", provider="zoho", address=address, status="active"
        )
        client = _FakeClient()
        return ZohoSender(None, connection, client=client), client

    def _email(self, **kwargs):
        from juicebox.outreach.sender import OutboundEmail

        base = dict(
            to="candidate@example.com",
            subject="s",
            text_body="b",
            html_body="",
            from_email="anna@goforay.io",
        )
        base.update(kwargs)
        return OutboundEmail(**base)

    def test_flattens_a_sub_address_of_itself(self):
        """A sub-address is the same mailbox and would be safe to allow, but
        Zoho refuses it: 500 "Given FromAddress not exists!" (verified live).
        So the relay's outbound leg loses per-candidate attribution -- inbound,
        where the employer writes to anna+token@, is unaffected.
        """
        zoho, client = self._sender()
        zoho.send(self._email(from_email="anna+c0ffee@goforay.io"))
        assert client.sent[0]["fromAddress"] == "anna@goforay.io"

    def test_refuses_a_different_mailbox(self):
        zoho, client = self._sender()
        zoho.send(self._email(from_email="someone@elsewhere.com"))
        assert client.sent[0]["fromAddress"] == "anna@goforay.io"

    def test_refuses_a_lookalike_local_part(self):
        """`annabel@` starts with `anna` but is not a sub-address of it."""
        zoho, client = self._sender()
        zoho.send(self._email(from_email="annabel@goforay.io"))
        assert client.sent[0]["fromAddress"] == "anna@goforay.io"

    def test_never_forwards_reply_to(self):
        """Zoho refuses the whole send unless Reply-To is verified on the
        account, and the relay pins it to the employer's address -- so
        forwarding it would fail every relayed message, not degrade it.
        Verified against live Zoho: 500 "You need to verify the ReplyTo
        address to send emails using this account."
        """
        zoho, client = self._sender()
        zoho.send(self._email(headers={"Reply-To": "recruiter@employer.com"}))
        assert "replyTo" not in client.sent[0]
