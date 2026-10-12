"""Domain checks: SPF, DMARC, DKIM key size, MX, public blocklists.

From the failure list (items 18-20): a resolver that refuses us is not a
listing, a timeout is not a missing record, and consumer domains are Google's
and Yahoo's business, not ours.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from juicebox.deliverability.dns_checks import (
    LookupFailed,
    NoRecord,
    check_domain,
    is_consumer_domain,
)


def _dkim_record(bits: int) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return f"v=DKIM1; k=rsa; p={base64.b64encode(der).decode()}"


@pytest.fixture(scope="module")
def dkim_1024():
    return _dkim_record(1024)


@pytest.fixture(scope="module")
def dkim_2048():
    return _dkim_record(2048)


def resolver(records: dict, *, failing: set[str] = frozenset()):
    """records: {(name, rdtype): [answers]}; anything absent is NoRecord."""

    def resolve(name, rdtype):
        if name in failing:
            raise LookupFailed(f"timeout resolving {name}")
        try:
            return list(records[(name, rdtype)])
        except KeyError:
            raise NoRecord(name) from None

    return resolve


def healthy(dkim):
    return {
        ("goforay.io", "MX"): ["10 mx.zoho.com."],
        ("goforay.io", "TXT"): ["v=spf1 include:zoho.com ~all", "google-site-verification=x"],
        ("_dmarc.goforay.io", "TXT"): ["v=DMARC1; p=quarantine; rua=mailto:d@goforay.io"],
        ("zmail._domainkey.goforay.io", "TXT"): [dkim],
    }


def _check(records, **kw):
    return check_domain(
        "goforay.io", resolve=resolver(records, **kw), selectors=["zmail", "google"]
    )


class TestAuthentication:
    def test_a_healthy_domain(self, dkim_2048):
        result = _check(healthy(dkim_2048))
        assert result["mx"]["status"] == "ok"
        assert result["spf"]["status"] == "ok"
        assert result["dmarc"] == {
            "status": "ok",
            "policy": "quarantine",
            "record": "v=DMARC1; p=quarantine; rua=mailto:d@goforay.io",
        }
        assert result["dkim"]["status"] == "ok"
        assert result["dkim"]["selectors"] == [{"selector": "zmail", "bits": 2048}]

    def test_a_1024_bit_dkim_key_is_weak(self, dkim_1024):
        result = _check(healthy(dkim_1024))
        assert result["dkim"]["status"] == "weak"
        assert result["dkim"]["selectors"][0]["bits"] == 1024

    def test_no_spf(self, dkim_2048):
        records = healthy(dkim_2048)
        records[("goforay.io", "TXT")] = ["google-site-verification=x"]
        assert _check(records)["spf"]["status"] == "missing"

    def test_two_spf_records_is_a_permerror(self, dkim_2048):
        records = healthy(dkim_2048)
        records[("goforay.io", "TXT")] = ["v=spf1 include:zoho.com ~all", "v=spf1 -all"]
        assert _check(records)["spf"]["status"] == "invalid"

    def test_plus_all_is_invalid(self, dkim_2048):
        records = healthy(dkim_2048)
        records[("goforay.io", "TXT")] = ["v=spf1 +all"]
        assert _check(records)["spf"]["status"] == "invalid"

    def test_dmarc_none_is_weak(self, dkim_2048):
        records = healthy(dkim_2048)
        records[("_dmarc.goforay.io", "TXT")] = ["v=DMARC1; p=none"]
        assert _check(records)["dmarc"]["status"] == "weak"

    def test_no_dkim_on_any_selector(self, dkim_2048):
        records = healthy(dkim_2048)
        del records[("zmail._domainkey.goforay.io", "TXT")]
        assert _check(records)["dkim"]["status"] == "missing"

    def test_a_timeout_is_unknown_not_missing(self, dkim_2048):
        result = _check(healthy(dkim_2048), failing={"_dmarc.goforay.io"})
        assert result["dmarc"]["status"] == "unknown"


class TestBlocklists:
    def _lists(self, records):
        return {b["name"]: b for b in _check(records)["blocklists"]}

    def test_clean_when_not_listed(self, dkim_2048):
        lists = self._lists(healthy(dkim_2048))
        assert {b["status"] for b in lists.values()} == {"clean"}

    def test_listed_on_spamhaus(self, dkim_2048):
        records = healthy(dkim_2048)
        records[("goforay.io.dbl.spamhaus.org", "A")] = ["127.0.1.2"]
        assert self._lists(records)["Spamhaus DBL"]["status"] == "listed"

    def test_spamhaus_refusing_our_resolver_is_unknown(self, dkim_2048):
        records = healthy(dkim_2048)
        records[("goforay.io.dbl.spamhaus.org", "A")] = ["127.255.255.254"]
        assert self._lists(records)["Spamhaus DBL"]["status"] == "unknown"

    def test_surbl_blocked_query_is_unknown_but_a_bitmask_is_listed(self, dkim_2048):
        records = healthy(dkim_2048)
        records[("goforay.io.multi.surbl.org", "A")] = ["127.0.0.1"]
        assert self._lists(records)["SURBL"]["status"] == "unknown"
        records[("goforay.io.multi.surbl.org", "A")] = ["127.0.0.16"]
        assert self._lists(records)["SURBL"]["status"] == "listed"

    def test_a_lookup_failure_is_unknown(self, dkim_2048):
        result = _check(healthy(dkim_2048), failing={"goforay.io.dbl.spamhaus.org"})
        lists = {b["name"]: b for b in result["blocklists"]}
        assert lists["Spamhaus DBL"]["status"] == "unknown"


def test_consumer_domains_are_not_ours_to_check():
    assert is_consumer_domain("gmail.com")
    assert is_consumer_domain("Yahoo.com")
    assert not is_consumer_domain("goforay.io")
