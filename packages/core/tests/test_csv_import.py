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
    def test_a_row_lands_as_a_candidate_with_its_emails_in_order(self, client, account):
        response = client.post(
            "/v1/candidates/bulk",
            headers=account["headers"],
            json={
                "candidates": [
                    csv_payload(
                        source_id="li:ada-lovelace",
                        full_name="Ada Lovelace",
                        emails=["ada@gmail.com", "ada@meta.com"],
                        reach={"ada@gmail.com": 90, "ada@meta.com": 40},
                        location="San Francisco Bay Area, California, United States",
                        linkedin_url="https://linkedin.com/in/ada-lovelace",
                        current_title="Research Engineer",
                        current_company="Meta",
                        years_experience=9.5,
                    )
                ]
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == 1 and body["updated"] == 0
        assert len(body["ids"]) == 1

        detail = client.get(
            f"/v1/candidates/{body['ids'][0]}", headers=account["headers"]
        ).json()
        assert detail["full_name"] == "Ada Lovelace"
        assert detail["current_company"] == "Meta"
        # Order is the whole point: emails[0] is what send_step mails, and the
        # personal address has to be the one in front.
        assert detail["emails"] == ["ada@gmail.com", "ada@meta.com"]

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

    def test_a_row_with_no_name_is_dropped_rather_than_failing_the_batch(self, client, account):
        """normalize_candidate raises on a nameless row and upsert skips it. The
        importer filters these client-side, but a partial batch must not 500."""
        response = client.post(
            "/v1/candidates/bulk",
            headers=account["headers"],
            json={
                "candidates": [
                    csv_payload(source_id="li:a", full_name="", emails=["a@x.com"]),
                    csv_payload(source_id="li:b", full_name="Real Person", emails=["b@x.com"]),
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["created"] == 1

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

    def test_enrolling_marks_the_import_as_tracked_in_the_crm(
        self, client, account, session, outreach_fixture
    ):
        candidate_id = self._import(
            client, account, source_id="li:cy", full_name="Cy Young", emails=["cy@x.com"]
        )
        enroll_candidates(
            session, sequence=outreach_fixture["sequence"], candidate_ids=[candidate_id]
        )
        session.commit()
        candidate = session.get(Candidate, candidate_id)
        # enroll_candidates calls track_all: choosing to email someone is what
        # makes their replies worth keeping.
        assert candidate.crm_tracked is True

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

    def test_a_csv_score_is_read(self, candidate):
        from juicebox.outreach.sequencer import _email_reach_scores

        candidate.raw = {"juicebox_csv": {"email_reach": {"a@x.com": 90}}}
        assert _email_reach_scores(candidate) == {"a@x.com": 90}

    def test_foray_still_wins_a_conflict(self, candidate):
        from juicebox.outreach.sequencer import _email_reach_scores

        candidate.raw = {
            "juicebox_csv": {"email_reach": {"a@x.com": 90}},
            "foray": {"email_reach": {"a@x.com": 5}},
        }
        assert _email_reach_scores(candidate) == {"a@x.com": 5}

    def test_a_namespace_that_is_not_a_dict_is_ignored(self, candidate):
        from juicebox.outreach.sequencer import _email_reach_scores

        candidate.raw = {"junk": "not a dict", "other": {"email_reach": "nor this"}}
        assert _email_reach_scores(candidate) == {}

    def test_no_raw_at_all_is_empty_rather_than_an_error(self, candidate):
        from juicebox.outreach.sequencer import _email_reach_scores

        candidate.raw = {}
        assert _email_reach_scores(candidate) == {}
