from __future__ import annotations

from foray.enrich.apollo import _usable
from foray.enrich.emails import extract_own_commit_emails, is_usable_commit_email
from foray.enrich.employer import domain_of, freemail_domains, is_work_domain


def _commit(email: str) -> dict:
    return {"commit": {"author": {"name": "whoever", "email": email}}}


def test_own_commit_emails_counted():
    commits = [_commit("jane@stripe.com"), _commit("jane@stripe.com"), _commit("jd@gmail.com")]
    assert extract_own_commit_emails(commits) == {"jane@stripe.com": 2, "jd@gmail.com": 1}


def test_noreply_and_bots_filtered():
    commits = [
        _commit("12345+janedoe@users.noreply.github.com"),
        _commit("noreply@github.com"),
        _commit("actions@github.com"),
        _commit("dependabot@example.com"),
        _commit("not-an-email"),
    ]
    assert extract_own_commit_emails(commits) == {}


def test_apollo_placeholder_rejected():
    assert _usable("email_not_unlocked@stripe.com") is None
    assert _usable("Jane.Doe@Stripe.com ") == "jane.doe@stripe.com"
    assert _usable("") is None
    assert _usable(None) is None


def test_domain_of():
    assert domain_of("Jane.Doe@Stripe.com ") == "stripe.com"
    assert domain_of("not-an-email") is None
    assert domain_of("") is None
    assert domain_of(None) is None  # type: ignore[arg-type]


def test_is_work_domain_uses_freemail_list():
    assert "gmail.com" in freemail_domains()  # sanity: the data file loads
    assert not is_work_domain("someone@gmail.com")
    assert is_work_domain("someone@stripe.com")
    assert not is_work_domain("garbage")


def test_usable_commit_email_keeps_incident_addresses():
    assert is_usable_commit_email("mail@danijar.com")
    assert is_usable_commit_email("email@jamie.sh")
    assert is_usable_commit_email("mail@taeuk.me")
    assert not is_usable_commit_email("123+ada@users.noreply.github.com")
    assert not is_usable_commit_email("dependabot@example.com")
