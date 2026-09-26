"""Filler addresses, and getting them back out of records that already have them.

The incident: ContactOut's quota ran out and it answered HTTP 200 with a sample
profile, so 30 imported candidates were given email1@example.com and
email2@gmail.com. The gmail one sorted first, which makes it primary_email, which
is what send_step sends to. Nothing went out, but only because nobody enrolled
them in time.
"""

from __future__ import annotations

import pytest

from juicebox.enrich.normalize import is_placeholder, strip_placeholders


class TestIsPlaceholder:
    @pytest.mark.parametrize(
        "email",
        [
            # The exact pair that reached production.
            "email1@example.com",
            "email2@gmail.com",
            # RFC 2606 / 6761: reserved so they reach nobody.
            "a.real.looking.name@example.com",
            "someone@example.net",
            "someone@example.org",
            "someone@box.invalid",
            "someone@thing.test",
            # Numbered filler: what a generated sample looks like.
            "test1@outlook.com",
            "user2@gmail.com",
            "mail3@yahoo.com",
            # Filler on any domain.
            "your-email@databricks.com",
            "firstlast@stripe.com",
            "noreply@stripe.com",
            "sample@databricks.com",
            # Bare filler on freemail: nobody had to own anything to pick it.
            "email@gmail.com",
            "test@gmail.com",
            "",
            "not-an-address",
        ],
    )
    def test_rejects_filler(self, email):
        assert is_placeholder(email) is True

    @pytest.mark.parametrize(
        "email",
        [
            # The five genuine addresses from the same enrichment run.
            "tara.lall@postman.com",
            "bagauntlett@mac.com",
            "pegedwards2011@gmail.com",
            "alex@streczyn.com",
            "taralall10@gmail.com",
            # Real addresses already in the database that a first draft of this
            # rule would have deleted. mail@ on your own domain is a normal
            # choice, and these were mined from git history.
            "mail@danijar.com",
            "email@jamie.sh",
            "mail@taeuk.me",
            "mail@kanth.tech",
            "email@peterswanson.tech",
            # Merely containing a filler word is not being one.
            "emailme@gmail.com",
            "testa.rossa@gmail.com",
            "samantha@gmail.com",
            "nameer@gmail.com",
        ],
    )
    def test_keeps_real_addresses(self, email):
        assert is_placeholder(email) is False

    def test_case_and_whitespace_do_not_evade_it(self):
        assert is_placeholder("  Email1@EXAMPLE.com  ") is True


class TestStripPlaceholders:
    def test_splits_and_preserves_order(self):
        keep, drop = strip_placeholders(
            ["email2@gmail.com", "real@stripe.com", "email1@example.com", "me@gmail.com"]
        )
        assert keep == ["real@stripe.com", "me@gmail.com"]
        assert drop == ["email2@gmail.com", "email1@example.com"]


class TestPurgeCommand:
    """The cleanup path, including the projection the other CRM work added."""

    def _candidate(self, client, account, emails, name="Yash Gupta"):
        response = client.post(
            "/v1/candidates",
            json={"full_name": name, "emails": emails, "source": "foray"},
            headers=account["headers"],
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def test_a_dry_run_writes_nothing(self, client, account, session, capsys):
        from juicebox.cli import main

        candidate_id = self._candidate(
            client, account, ["email2@gmail.com", "email1@example.com"]
        )
        assert main(["purge-placeholder-emails"]) == 0
        out = capsys.readouterr().out
        assert "would drop" in out
        assert "re-run with --apply" in out

        response = client.get(f"/v1/candidates/{candidate_id}", headers=account["headers"])
        assert response.json()["emails"] == ["email2@gmail.com", "email1@example.com"]

    def test_apply_removes_only_the_filler(self, client, account, capsys):
        from juicebox.cli import main

        candidate_id = self._candidate(
            client, account, ["email2@gmail.com", "yash@stripe.com", "email1@example.com"]
        )
        assert main(["purge-placeholder-emails", "--apply"]) == 0
        assert "removed 2 address(es) from 1 candidate(s)" in capsys.readouterr().out

        response = client.get(f"/v1/candidates/{candidate_id}", headers=account["headers"])
        assert response.json()["emails"] == ["yash@stripe.com"]

    def test_the_address_projection_is_rebuilt(self, client, account, session):
        """candidate_emails is a subset of the JSON column and drifts silently if
        a writer forgets it -- which would leave the filler address still
        resolving to this person in the duplicate check."""
        from sqlalchemy import select

        from juicebox.cli import main
        from juicebox.models import CandidateEmail

        candidate_id = self._candidate(client, account, ["email2@gmail.com", "ok@stripe.com"])
        main(["purge-placeholder-emails", "--apply"])

        session.expire_all()
        rows = session.scalars(
            select(CandidateEmail).where(CandidateEmail.candidate_id == candidate_id)
        ).all()
        assert sorted(row.email for row in rows) == ["ok@stripe.com"]
