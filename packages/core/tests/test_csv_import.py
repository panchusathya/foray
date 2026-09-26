"""The server half of the CSV import.

The mapping from a Juicebox export row to a ``CandidateIn`` lives in the browser
(``packages/web/src/lib/csvImport.js``) and is unit-tested there. What those
tests cannot check is whether the server accepts the shape they emit and whether
the sequencer then mails the address the mapper chose -- which is the only part
a recruiter would notice going wrong. So these payloads are written out
verbatim, exactly as ``juiceboxRowToCandidate`` produces them, rather than built
from a helper: if the JS changes and this does not, the drift should fail here.
"""

from __future__ import annotations

import pytest

from juicebox.config import reset_settings_cache
from juicebox.models import Candidate, CandidateEmail, Enrollment
from juicebox.outreach.sequencer import enroll_candidates, run_due

CSV_SOURCE = "juicebox_csv"


def csv_payload(
    *,
    source_id: str,
    full_name: str,
    emails: list[str],
    reach: dict[str, int] | None = None,
    excluded: list[dict] | None = None,
    **extra,
) -> dict:
    """One row as the browser sends it, nested namespace and all."""
    payload = {
        "source": CSV_SOURCE,
        "source_id": source_id,
        "full_name": full_name,
        "emails": emails,
        "raw": {
            "juicebox_csv": {
                "email_reach": reach or {},
                "excluded_emails": excluded or [],
                "education": "",
                "profile_url": "",
            }
        },
    }
    payload.update(extra)
    return payload


class TestBulkAcceptsTheExportShape:

    def test_the_same_export_row_twice_updates_rather_than_duplicates(self, client, account):
        row = csv_payload(
            source_id="li:ada-lovelace",
            full_name="Ada Lovelace",
            emails=["ada@gmail.com"],
            reach={"ada@gmail.com": 90},
        )
        first = client.post(
            "/v1/candidates/bulk", headers=account["headers"], json={"candidates": [row]}
        ).json()
        # The real case this covers: two overlapping exports downloaded a day
        # apart, which is exactly what the sample files in Downloads are.
        second = client.post(
            "/v1/candidates/bulk", headers=account["headers"], json={"candidates": [row]}
        ).json()
        assert first["created"] == 1
        assert second["created"] == 0 and second["updated"] == 1
        assert first["ids"] == second["ids"]

    def test_the_email_projection_is_populated_so_reply_matching_works(
        self, client, account, session
    ):
        client.post(
            "/v1/candidates/bulk",
            headers=account["headers"],
            json={
                "candidates": [
                    csv_payload(
                        source_id="li:bo",
                        full_name="Bo Diddley",
                        emails=["bo@gmail.com", "bo@acme.com"],
                    )
                ]
            },
        )
        rows = session.query(CandidateEmail).filter_by(org_id=account["org_id"]).all()
        assert {r.email for r in rows} == {"bo@gmail.com", "bo@acme.com"}

    def test_a_duplicate_source_id_inside_one_batch_does_not_trip_the_unique_key(
        self, client, account
    ):
        row = csv_payload(source_id="li:same", full_name="Same Person", emails=["s@x.com"])
        response = client.post(
            "/v1/candidates/bulk",
            headers=account["headers"],
            json={"candidates": [row, dict(row)]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["created"] == 1


class TestSequencingACsvImport:
    """The point of the feature: a CSV-sourced candidate sequences identically."""

    def _import(self, client, account, **kwargs) -> str:
        body = client.post(
            "/v1/candidates/bulk",
            headers=account["headers"],
            json={"candidates": [csv_payload(**kwargs)]},
        ).json()
        assert body["ids"], body
        return body["ids"][0]

    def test_an_imported_candidate_gets_the_first_email_at_their_personal_address(
        self, client, account, session, outreach_fixture, mailbox
    ):
        candidate_id = self._import(
            client,
            account,
            source_id="li:ada",
            full_name="Ada Lovelace",
            emails=["ada@gmail.com", "ada@meta.com"],
            reach={"ada@gmail.com": 90, "ada@meta.com": 40},
        )
        enroll_candidates(
            session,
            sequence=outreach_fixture["sequence"],
            candidate_ids=[candidate_id],
            send_now=True,
        )
        session.commit()
        summary = run_due(session, org_id=account["org_id"])
        session.commit()

        assert summary.sent >= 1
        assert "ada@gmail.com" in [message.to for message in mailbox.outbox]

    def test_a_risky_only_import_is_stopped_when_the_floor_is_raised(
        self, client, account, session, outreach_fixture, mailbox, monkeypatch
    ):
        """The verification column has to reach the existing floor.

        This is the test that would have caught the namespace bug: the scores
        are written under ``raw.juicebox_csv``, and the floor used to read
        ``raw.foray`` alone, so a Risky address sailed through.
        """
        monkeypatch.setenv("FORAY_MIN_REACH_SCORE", "50")
        reset_settings_cache()
        candidate_id = self._import(
            client,
            account,
            source_id="li:risky",
            full_name="Risky Rick",
            emails=["risky@x.com"],
            reach={"risky@x.com": 40},
        )
        enroll_candidates(
            session, sequence=outreach_fixture["sequence"], candidate_ids=[candidate_id]
        )
        session.commit()
        run_due(session, org_id=account["org_id"])
        session.commit()

        enrollment = session.query(Enrollment).filter_by(candidate_id=candidate_id).one()
        assert enrollment.status == "stopped"
        assert enrollment.stopped_reason == "reach_score_below_floor"
        assert "risky@x.com" not in [m.to for m in mailbox.outbox]

    def test_the_floor_skips_a_risky_address_for_a_deliverable_one(
        self, client, account, session, outreach_fixture, mailbox, monkeypatch
    ):
        monkeypatch.setenv("FORAY_MIN_REACH_SCORE", "50")
        reset_settings_cache()
        candidate_id = self._import(
            client,
            account,
            source_id="li:mixed",
            full_name="Mixed Mary",
            emails=["risky@gmail.com", "good@acme.com"],
            reach={"risky@gmail.com": 40, "good@acme.com": 90},
        )
        enroll_candidates(
            session,
            sequence=outreach_fixture["sequence"],
            candidate_ids=[candidate_id],
            send_now=True,
        )
        session.commit()
        run_due(session, org_id=account["org_id"])
        session.commit()
        assert "good@acme.com" in [m.to for m in mailbox.outbox]


class TestReachScoresAcrossNamespaces:
    """``_email_reach_scores`` had to stop being Foray-only. Foray still wins."""

    @pytest.fixture
    def candidate(self, session, account):
        row = Candidate(
            org_id=account["org_id"],
            source=CSV_SOURCE,
            source_id="li:ns",
            full_name="Namespace Ned",
            emails=["a@x.com"],
        )
        session.add(row)
        session.flush()
        return row

    def test_foray_still_wins_a_conflict(self, candidate):
        from juicebox.outreach.sequencer import _email_reach_scores

        candidate.raw = {
            "juicebox_csv": {"email_reach": {"a@x.com": 90}},
            "foray": {"email_reach": {"a@x.com": 5}},
        }
        assert _email_reach_scores(candidate) == {"a@x.com": 5}


class TestCallerAttributionAfterAnImport:
    """The reason phones are imported at all.

    ``candidate_phones`` is what lets an inbound call be attributed, so a
    candidate imported from a CSV with a number must be findable by the number
    a voice platform hands us -- in whatever shape it hands it over.
    """

    def test_an_imported_number_is_matchable_in_every_format_a_provider_might_send(
        self, client, account, session
    ):
        from juicebox.intake.service import resolve_candidate_id

        body = client.post(
            "/v1/candidates/bulk",
            headers=account["headers"],
            json={
                "candidates": [
                    csv_payload(
                        source_id="li:ada",
                        full_name="Ada Lovelace",
                        emails=["ada@gmail.com"],
                        phones=["+1 213-431-7465"],
                    )
                ]
            },
        ).json()
        candidate_id = body["ids"][0]

        # Every one of these is the same person. Before resolve_candidate_id
        # normalised its input, only the tidy E.164 form matched and the rest
        # silently produced an unattributed call.
        for shape in (
            "+12134317465",
            "12134317465",
            "2134317465",
            "(213) 431-7465",
            "213-431-7465",
        ):
            assert (
                resolve_candidate_id(session, org_id=account["org_id"], phone=shape)
                == candidate_id
            ), shape

    def test_an_unmatchable_number_still_matches_nobody(self, client, account, session):
        """lookup_key refuses to guess a country, and that must survive the fix:
        a wrong phone match discloses somebody else's record."""
        from juicebox.intake.service import resolve_candidate_id

        client.post(
            "/v1/candidates/bulk",
            headers=account["headers"],
            json={
                "candidates": [
                    csv_payload(
                        source_id="li:bo",
                        full_name="Bo Diddley",
                        emails=["bo@x.com"],
                        phones=["+1 415-555-0123"],
                    )
                ]
            },
        )
        for junk in ("", "12345", "987654321", "not a phone"):
            assert resolve_candidate_id(session, org_id=account["org_id"], phone=junk) is None, junk

    def test_a_number_from_another_org_is_never_matched(self, client, account, session):
        from juicebox.intake.service import resolve_candidate_id

        client.post(
            "/v1/candidates/bulk",
            headers=account["headers"],
            json={
                "candidates": [
                    csv_payload(
                        source_id="li:cy",
                        full_name="Cy Young",
                        emails=["cy@x.com"],
                        phones=["+12134317465"],
                    )
                ]
            },
        )
        assert resolve_candidate_id(session, org_id="org_someone_else", phone="+12134317465") is None
