"""Password hashing: configured cost on write, stored cost on verify."""

from __future__ import annotations

from juicebox.config import reset_settings_cache
from juicebox.security import hash_password, verify_password


class TestPasswordHash:
    def test_hash_embeds_the_configured_round_count(self, monkeypatch):
        monkeypatch.setenv("JUICEBOX_PBKDF2_ROUNDS", "3")
        reset_settings_cache()
        encoded = hash_password("supersecret1")
        assert encoded.startswith("pbkdf2_sha256$3$")
        assert verify_password("supersecret1", encoded)
        assert not verify_password("wrong-password", encoded)

    def test_verify_uses_the_stored_rounds_not_the_current_setting(self, monkeypatch):
        monkeypatch.setenv("JUICEBOX_PBKDF2_ROUNDS", "2")
        reset_settings_cache()
        encoded = hash_password("supersecret1")

        monkeypatch.setenv("JUICEBOX_PBKDF2_ROUNDS", "1000")
        reset_settings_cache()
        assert encoded.startswith("pbkdf2_sha256$2$")
        assert verify_password("supersecret1", encoded)

        cheap = hash_password("supersecret1")
        assert cheap.startswith("pbkdf2_sha256$1000$")
        assert verify_password("supersecret1", cheap)

    def test_malformed_hash_does_not_verify(self):
        assert verify_password("x", "not-a-hash") is False
        assert verify_password("x", "pbkdf2_sha256$not-int$ab$cd") is False
