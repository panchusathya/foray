from __future__ import annotations

from foray.enrich.employer import domain_of, freemail_domains, is_work_domain


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
