"""`/health` has to say when the phone line cannot record anything.

Prod ran for a day with no ``RETELL_API_KEY``: every webhook was refused with a
401, calls happened and cost money, and nothing was stored. The only evidence
was one log line per delivery. A health field turns that into something a person
or a monitor notices.
"""

from __future__ import annotations

from juicebox.config import reset_settings_cache
from juicebox.intake.providers import FakeVoiceProvider, set_voice_provider
from juicebox.main import _voice_webhooks_ready


class TestVoiceWebhookReadiness:
    def test_a_missing_key_reads_as_not_ready(self, monkeypatch):
        monkeypatch.setenv("RETELL_API_KEY", "")
        reset_settings_cache()
        set_voice_provider(None)
        try:
            assert _voice_webhooks_ready() is False
        finally:
            set_voice_provider(None)
            reset_settings_cache()

    def test_a_present_key_reads_as_ready(self, monkeypatch):
        monkeypatch.setenv("RETELL_API_KEY", "key_whatever_this_is_not_validated")
        reset_settings_cache()
        set_voice_provider(None)
        try:
            assert _voice_webhooks_ready() is True
        finally:
            set_voice_provider(None)
            reset_settings_cache()

    def test_a_provider_needing_no_key_is_ready(self):
        """The fake provider verifies nothing, and must not read as broken."""
        set_voice_provider(FakeVoiceProvider())
        try:
            assert _voice_webhooks_ready() is True
        finally:
            set_voice_provider(None)

    def test_health_reports_the_field(self, client):
        body = client.get("/health").json()
        assert "voice_webhooks" in body
        assert isinstance(body["voice_webhooks"], bool)

    def test_health_never_500s_on_a_broken_provider(self, client):
        class Exploding:
            name = "exploding"

            def verify(self, *, raw_body, signature):
                raise RuntimeError("boom")

        set_voice_provider(Exploding())
        try:
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json()["voice_webhooks"] is False
        finally:
            set_voice_provider(None)
