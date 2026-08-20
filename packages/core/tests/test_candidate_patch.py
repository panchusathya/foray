"""Editing a person by hand, without losing anything.

Two failure modes drive these tests, both of which are quiet:

- a title edit that deletes and recreates the employment history, because it
  went through the importer's write path;
- a nightly refresh that reverts the edit, which looks to the user like the
  edit never saved rather than like a bug.
"""

from __future__ import annotations

from sqlalchemy import select

from juicebox.models import Candidate, Suppression
from juicebox.repository import upsert_candidates


def _foray_payload(**overrides) -> dict:
    payload = {
        "source": "foray",
        "source_id": "ada-lovelace",
        "full_name": "Ada Lovelace",
        "current_title": "Senior Engineer",
        "current_company": "Analytical Engines",
        "emails": ["ada@analytical.example.com"],
        "skills": ["python"],
        "experiences": [
            {"company": "Analytical Engines", "title": "Senior Engineer", "start_date": "2020-01"},
            {"company": "Difference Machines", "title": "Engineer", "start_date": "2016-01"},
        ],
        "educations": [{"school": "Cambridge", "degree": "BA"}],
    }
    payload.update(overrides)
    return payload


def _seed(session, org_id) -> Candidate:
    result = upsert_candidates(session, org_id=org_id, payloads=[_foray_payload()])
    # Commit, not flush: SQLite locks the file for an open write transaction,
    # and every test here calls the API through TestClient's own connection.
    session.commit()
    return session.get(Candidate, result.ids[0])


def _reimport(session, org_id, **overrides) -> None:
    upsert_candidates(session, org_id=org_id, payloads=[_foray_payload(**overrides)])
    session.commit()
    session.expire_all()


class TestNonDestructive:
    def test_patching_a_title_keeps_the_experience_rows_intact(
        self, client, session, account
    ):
        """The headline regression. upsert_candidates would delete and recreate
        these with new ids; a job-title correction must not rewrite someone's
        employment history."""
        candidate = _seed(session, account["org_id"])
        before = sorted(e.id for e in candidate.experiences)
        education_before = sorted(e.id for e in candidate.educations)
        assert len(before) == 2

        response = client.patch(
            f"/v1/candidates/{candidate.id}",
            json={"current_title": "Staff Engineer"},
            headers=account["headers"],
        )
        assert response.status_code == 200, response.text

        session.expire_all()
        candidate = session.get(Candidate, candidate.id)
        assert candidate.current_title == "Staff Engineer"
        assert sorted(e.id for e in candidate.experiences) == before
        assert sorted(e.id for e in candidate.educations) == education_before

    def test_absent_means_untouched_and_empty_means_clear(
        self, client, session, account
    ):
        candidate = _seed(session, account["org_id"])

        client.patch(
            f"/v1/candidates/{candidate.id}",
            json={"headline": ""},
            headers=account["headers"],
        )
        session.expire_all()
        candidate = session.get(Candidate, candidate.id)
        assert candidate.headline == ""
        # Never sent, so untouched.
        assert candidate.current_company == "Analytical Engines"

    def test_a_renamed_title_is_findable_under_the_new_one(
        self, client, session, account
    ):
        """The derived columns have to follow, or the person stays searchable
        only under the title they no longer hold."""
        candidate = _seed(session, account["org_id"])

        client.patch(
            f"/v1/candidates/{candidate.id}",
            json={"current_title": "Head of Platform"},
            headers=account["headers"],
        )
        session.expire_all()
        candidate = session.get(Candidate, candidate.id)
        assert candidate.current_title_normalized


class TestManualFieldsSurviveReimport:
    def test_a_hand_edit_beats_the_next_import(self, client, session, account):
        candidate = _seed(session, account["org_id"])
        client.patch(
            f"/v1/candidates/{candidate.id}",
            json={"current_title": "Head of Platform"},
            headers=account["headers"],
        )
        session.expire_all()

        # Foray runs again with its original view of the world.
        _reimport(session, account["org_id"])

        candidate = session.get(Candidate, candidate.id)
        assert candidate.current_title == "Head of Platform"
        assert "current_title" in candidate.manual_fields

    def test_fields_nobody_edited_still_update(self, client, session, account):
        """Protection is per field. A refresh must still be a refresh."""
        candidate = _seed(session, account["org_id"])
        client.patch(
            f"/v1/candidates/{candidate.id}",
            json={"current_title": "Head of Platform"},
            headers=account["headers"],
        )
        session.expire_all()

        _reimport(session, account["org_id"], current_company="New Employer")

        candidate = session.get(Candidate, candidate.id)
        assert candidate.current_title == "Head of Platform"
        assert candidate.current_company == "New Employer"

    def test_a_corrected_email_does_not_block_newly_found_ones(
        self, client, session, account
    ):
        """Emails union rather than freeze: correcting an address should not
        stop enrichment ever finding another."""
        candidate = _seed(session, account["org_id"])
        client.patch(
            f"/v1/candidates/{candidate.id}",
            json={"emails": ["ada@personal.example.com"]},
            headers=account["headers"],
        )
        session.expire_all()

        _reimport(session, account["org_id"], emails=["ada@newjob.example.com"])

        candidate = session.get(Candidate, candidate.id)
        assert "ada@personal.example.com" in candidate.emails
        assert "ada@newjob.example.com" in candidate.emails

    def test_dropping_the_override_lets_the_source_win_again(
        self, client, session, account
    ):
        candidate = _seed(session, account["org_id"])
        client.patch(
            f"/v1/candidates/{candidate.id}",
            json={"current_title": "Head of Platform"},
            headers=account["headers"],
        )
        session.expire_all()

        candidate = session.get(Candidate, candidate.id)
        candidate.manual_fields = []
        session.commit()

        _reimport(session, account["org_id"])

        assert session.get(Candidate, candidate.id).current_title == "Senior Engineer"


class TestTracking:
    def test_a_profile_edit_does_not_start_retaining_their_mail(
        self, client, session, account
    ):
        """crm_tracked widens the thread-sync privacy guard. Fixing a typo is
        not consent to store somebody's mailbox history."""
        candidate = _seed(session, account["org_id"])

        client.patch(
            f"/v1/candidates/{candidate.id}",
            json={"current_title": "Staff Engineer"},
            headers=account["headers"],
        )
        session.expire_all()
        assert session.get(Candidate, candidate.id).crm_tracked is False

    def test_setting_a_status_is_an_explicit_crm_action(
        self, client, session, account
    ):
        candidate = _seed(session, account["org_id"])

        client.patch(
            f"/v1/candidates/{candidate.id}",
            json={"crm_status": "active"},
            headers=account["headers"],
        )
        session.expire_all()
        assert session.get(Candidate, candidate.id).crm_tracked is True

    def test_shortlisting_tracks_them(self, client, session, account, loaded_org):
        project = client.post(
            "/v1/projects", json={"name": "Platform"}, headers=account["headers"]
        ).json()
        candidate = session.scalars(select(Candidate)).first()

        client.post(
            f"/v1/projects/{project['id']}/candidates",
            json={"candidate_ids": [candidate.id]},
            headers=account["headers"],
        )
        session.expire_all()
        assert session.get(Candidate, candidate.id).crm_tracked is True


class TestDoNotContact:
    def test_it_writes_a_real_suppression(self, client, session, account):
        """A status the sequencer ignores is a promise the product breaks."""
        candidate = _seed(session, account["org_id"])

        client.patch(
            f"/v1/candidates/{candidate.id}",
            json={"crm_status": "do_not_contact"},
            headers=account["headers"],
        )
        session.expire_all()

        rows = session.scalars(select(Suppression)).all()
        assert [r.email for r in rows] == ["ada@analytical.example.com"]

    def test_moving_off_it_lifts_the_suppression(self, client, session, account):
        candidate = _seed(session, account["org_id"])
        for status_value in ("do_not_contact", "active"):
            client.patch(
                f"/v1/candidates/{candidate.id}",
                json={"crm_status": status_value},
                headers=account["headers"],
            )
        session.expire_all()

        assert session.scalars(select(Suppression)).all() == []

    def test_an_unknown_status_is_rejected(self, client, session, account):
        candidate = _seed(session, account["org_id"])
        response = client.patch(
            f"/v1/candidates/{candidate.id}",
            json={"crm_status": "vibing"},
            headers=account["headers"],
        )
        assert response.status_code == 422


class TestScoping:
    def test_another_org_cannot_patch_your_people(self, client, session, account):
        candidate = _seed(session, account["org_id"])
        other = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival",
                "email": "rival@rival-talent.com",
                "password": "supersecret1",
                "name": "Bo",
            },
        ).json()
        headers = {"Authorization": f"Bearer {other['access_token']}"}

        response = client.patch(
            f"/v1/candidates/{candidate.id}",
            json={"current_title": "Nope"},
            headers=headers,
        )
        assert response.status_code == 404
