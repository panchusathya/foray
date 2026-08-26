"""Authorization boundaries: cross-org ids, read-only principals, scoped tokens.

Every test here asserts a *refusal*. They exist because each of these holes was
one call site that skipped a check its siblings already made, so the coverage is
deliberately per-endpoint rather than per-feature.
"""

from __future__ import annotations

import pytest


def _rival(client, *, email: str) -> dict:
    """A second org with its own headers — the attacker in every test below."""
    payload = client.post(
        "/v1/auth/signup",
        json={"org_name": "Rival", "email": email, "password": "supersecret1"},
    ).json()
    return {
        "headers": {"Authorization": f"Bearer {payload['access_token']}"},
        "org_id": payload["user"]["org_id"],
        "user_id": payload["user"]["id"],
    }


def _make_viewer(session, user_id: str) -> None:
    from juicebox.models import User

    user = session.get(User, user_id)
    user.role = "viewer"
    session.commit()


class TestJobPostingProjectScope:
    """A posting may only ever point at a project in its own org.

    ``create-project`` reads ``posting.project_id`` straight back out as a
    ProjectOut — name, role title, the whole JD — so an unchecked write here is
    a read of another tenant's brief.
    """

    def test_creating_a_posting_against_another_orgs_project_is_rejected(
        self, client, account
    ):
        rival = _rival(client, email="rival-create@rival-talent.com")
        their_project = client.post(
            "/v1/projects", json={"name": "Rival role"}, headers=rival["headers"]
        ).json()

        response = client.post(
            "/v1/job-postings",
            json={"title": "Staff Engineer", "project_id": their_project["id"]},
            headers=account["headers"],
        )
        assert response.status_code == 404

    def test_patching_a_posting_onto_another_orgs_project_is_rejected(
        self, client, account
    ):
        rival = _rival(client, email="rival-patch@rival-talent.com")
        their_project = client.post(
            "/v1/projects",
            json={"name": "Rival role", "role_title": "Rival CTO"},
            headers=rival["headers"],
        ).json()
        posting = client.post(
            "/v1/job-postings",
            json={"title": "Staff Engineer"},
            headers=account["headers"],
        ).json()

        response = client.patch(
            f"/v1/job-postings/{posting['id']}",
            json={"project_id": their_project["id"]},
            headers=account["headers"],
        )
        assert response.status_code == 404
        after = client.get(
            f"/v1/job-postings/{posting['id']}", headers=account["headers"]
        ).json()
        assert after.get("project_id") in (None, "")

    def test_importing_against_another_orgs_project_is_rejected(self, client, account):
        rival = _rival(client, email="rival-import@rival-talent.com")
        their_project = client.post(
            "/v1/projects", json={"name": "Rival role"}, headers=rival["headers"]
        ).json()

        response = client.post(
            "/v1/job-postings/import",
            json={
                "postings": [
                    {"title": "Staff Engineer", "project_id": their_project["id"]}
                ]
            },
            headers=account["headers"],
        )
        assert response.status_code == 404

    def test_create_project_never_returns_another_orgs_project(
        self, client, account, session
    ):
        """Rows stamped before the write check existed must not leak either."""
        from juicebox.models import JobPosting

        rival = _rival(client, email="rival-leak@rival-talent.com")
        their_project = client.post(
            "/v1/projects",
            json={
                "name": "Rival secret search",
                "role_title": "Rival CTO",
                "description": "Confidential brief.",
            },
            headers=rival["headers"],
        ).json()
        posting = client.post(
            "/v1/job-postings",
            json={"title": "Staff Engineer"},
            headers=account["headers"],
        ).json()
        # Straight to the column, as legacy data would have it.
        session.get(JobPosting, posting["id"]).project_id = their_project["id"]
        session.commit()

        response = client.post(
            f"/v1/job-postings/{posting['id']}/create-project",
            json={},
            headers=account["headers"],
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] != their_project["id"]
        assert body["name"] == "Staff Engineer"
        assert "Confidential brief." not in body.get("description", "")

    def test_a_project_in_your_own_org_still_works(self, client, account):
        project = client.post(
            "/v1/projects", json={"name": "Our role"}, headers=account["headers"]
        ).json()
        response = client.post(
            "/v1/job-postings",
            json={"title": "Staff Engineer", "project_id": project["id"]},
            headers=account["headers"],
        )
        assert response.status_code == 201, response.text
        assert response.json()["project_id"] == project["id"]


class TestSequenceProjectScope:
    def test_creating_a_sequence_against_another_orgs_project_is_rejected(
        self, client, account
    ):
        """update_sequence and enroll already refuse this; create used not to,
        and /preview renders the project's role_title and location back out."""
        rival = _rival(client, email="rival-seq@rival-talent.com")
        their_project = client.post(
            "/v1/projects",
            json={"name": "Rival role", "role_title": "Rival CTO"},
            headers=rival["headers"],
        ).json()

        response = client.post(
            "/v1/sequences",
            json={
                "name": "Cross-tenant",
                "project_id": their_project["id"],
                "personalize": False,
                "steps": [{"delay_days": 0, "subject_template": "Hi", "body_template": "Hi"}],
            },
            headers=account["headers"],
        )
        assert response.status_code == 404

    def test_an_unknown_project_is_a_404_not_a_500(self, client, account):
        response = client.post(
            "/v1/sequences",
            json={
                "name": "Bogus",
                "project_id": "proj_does_not_exist",
                "personalize": False,
                "steps": [{"delay_days": 0, "subject_template": "Hi", "body_template": "Hi"}],
            },
            headers=account["headers"],
        )
        assert response.status_code == 404

    def test_your_own_project_still_pins(self, client, account):
        project = client.post(
            "/v1/projects", json={"name": "Our role"}, headers=account["headers"]
        ).json()
        response = client.post(
            "/v1/sequences",
            json={
                "name": "Fine",
                "project_id": project["id"],
                "personalize": False,
                "steps": [{"delay_days": 0, "subject_template": "Hi", "body_template": "Hi"}],
            },
            headers=account["headers"],
        )
        assert response.status_code == 201, response.text
        assert response.json()["project_id"] == project["id"]


class TestApiKeyPrivilege:
    """An API key authenticates as role "service", which can write. Minting one
    is therefore a privilege escalation for anybody who cannot already write."""

    def test_a_viewer_cannot_mint_a_key(self, client, account, session):
        _make_viewer(session, account["user_id"])
        response = client.post("/v1/auth/api-keys", headers=account["headers"])
        assert response.status_code == 403

    def test_a_viewer_cannot_revoke_a_key(self, client, account, session):
        created = client.post("/v1/auth/api-keys", headers=account["headers"]).json()
        _make_viewer(session, account["user_id"])
        response = client.delete(
            f"/v1/auth/api-keys/{created['id']}", headers=account["headers"]
        )
        assert response.status_code == 403
        assert client.get("/v1/candidates", headers={"X-API-Key": created["key"]}).status_code == 200

    def test_a_viewer_can_still_list_them(self, client, account, session):
        """Prefixes only, no key material — reading the inventory is not a write."""
        client.post("/v1/auth/api-keys", headers=account["headers"])
        _make_viewer(session, account["user_id"])
        response = client.get("/v1/auth/api-keys", headers=account["headers"])
        assert response.status_code == 200
        assert response.json()[0]["key"] is None


@pytest.fixture
def ms_configured(monkeypatch):
    monkeypatch.setenv("MS_CLIENT_ID", "client-123")
    monkeypatch.setenv("MS_CLIENT_SECRET", "secret-456")
    from juicebox.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


class TestSinglePurposeTokens:
    """The OAuth connect state is signed with the same key as a login token and
    then handed to the provider on a query string. It must not be a session."""

    def test_a_connect_state_is_not_a_bearer_token(
        self, client, account, ms_configured
    ):
        auth_url = client.post(
            "/v1/integrations/microsoft/connect", headers=account["headers"]
        ).json()["auth_url"]
        state = auth_url.split("state=")[1].split("&")[0]

        response = client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {state}"}
        )
        assert response.status_code == 401
        assert (
            client.get(
                "/v1/candidates", headers={"Authorization": f"Bearer {state}"}
            ).status_code
            == 401
        )

    def test_a_hand_minted_state_role_is_refused_too(self, client, account):
        from juicebox.deps import OAUTH_STATE_ROLE
        from juicebox.security import create_access_token

        token = create_access_token(
            subject=account["user_id"],
            org_id=account["org_id"],
            role=OAUTH_STATE_ROLE,
            ttl_seconds=600,
        )
        response = client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401

    def test_a_real_login_token_still_works(self, client, account):
        assert client.get("/v1/auth/me", headers=account["headers"]).status_code == 200


class TestClickRedirect:
    """``/t/c`` takes no auth and is served from the app's own origin, so it
    must never emit a Location it did not sign."""

    EVIL = "https://evil.example/login"

    def _message(self, session, account, outreach_fixture, mailbox):
        from juicebox.models import Message
        from juicebox.outreach.sequencer import enroll_candidates, run_due

        sequence = outreach_fixture["sequence"]
        enroll_candidates(
            session,
            sequence=sequence,
            candidate_ids=[outreach_fixture["candidates"][0].id],
        )
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()
        return session.query(Message).filter_by(direction="outbound").one()

    def test_an_unsigned_click_does_not_redirect_off_site(self, client):
        response = client.get(
            f"/t/c/msg_anything?u={self.EVIL}", follow_redirects=False
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/"

    def test_a_forged_token_does_not_redirect_off_site(
        self, client, session, account, outreach_fixture, mailbox
    ):
        from juicebox.models import Message

        message = self._message(session, account, outreach_fixture, mailbox)
        response = client.get(
            f"/t/c/{message.id}?k=forged&u={self.EVIL}", follow_redirects=False
        )
        assert response.headers["location"] == "/"

        session.expire_all()
        assert session.get(Message, message.id).clicked_at is None

    def test_a_url_bound_token_redirects_and_records(
        self, client, session, account, outreach_fixture, mailbox
    ):
        from juicebox.models import Message
        from juicebox.security import signed_intake_token

        message = self._message(session, account, outreach_fixture, mailbox)
        target = "https://jobs.example.com/role"
        token = signed_intake_token(f"click:{message.id}", target)

        response = client.get(
            f"/t/c/{message.id}", params={"k": token, "u": target}, follow_redirects=False
        )
        assert response.status_code == 302
        assert response.headers["location"] == target

        session.expire_all()
        assert session.get(Message, message.id).clicked_at is not None

    def test_a_url_bound_token_does_not_travel_to_another_url(
        self, client, session, account, outreach_fixture, mailbox
    ):
        from juicebox.security import signed_intake_token

        message = self._message(session, account, outreach_fixture, mailbox)
        token = signed_intake_token(f"click:{message.id}", "https://jobs.example.com/role")

        response = client.get(
            f"/t/c/{message.id}",
            params={"k": token, "u": self.EVIL},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/"

    def test_the_link_a_real_send_writes_is_url_bound(
        self, client, session, account, outreach_fixture, mailbox
    ):
        """The generator half. Verification accepting a url-bound token closes
        nothing while the send path still mints the legacy one, so this walks
        the link out of the delivered HTML and back through the endpoint."""
        import re
        from urllib.parse import parse_qs, unquote, urlparse

        # The shared fixture's copy carries no link, and a link is the whole
        # subject here. HTML, because to_html only rewrites real anchors.
        step = sorted(outreach_fixture["sequence"].steps, key=lambda s: s.step_order)[0]
        step.body_template = (
            '<p>Hi {{ first_name }},</p>'
            '<p><a href="https://jobs.example.com/role">the role</a></p>'
        )
        session.flush()

        message = self._message(session, account, outreach_fixture, mailbox)
        sent = mailbox.outbox[-1]
        hrefs = re.findall(r'href="([^"]+)"', sent.html_body or "")
        tracked = [h for h in hrefs if "/t/c/" in h]
        assert tracked, f"no tracked link in the sent body: {hrefs}"

        query = parse_qs(urlparse(tracked[0]).query)
        k, u = query["k"][0], unquote(query["u"][0])

        # It redirects where it says...
        response = client.get(
            f"/t/c/{message.id}", params={"k": k, "u": u}, follow_redirects=False
        )
        assert response.headers["location"] == u

        # ...and that same token cannot be pointed anywhere else, which is the
        # property the legacy shape could not give.
        swapped = client.get(
            f"/t/c/{message.id}", params={"k": k, "u": self.EVIL}, follow_redirects=False
        )
        assert swapped.headers["location"] == "/"

    def test_a_legacy_token_still_works(
        self, client, session, account, outreach_fixture, mailbox
    ):
        """Links already sitting in recipients' inboxes were signed over the
        message id alone; breaking them would break real mail."""
        from juicebox.models import Message
        from juicebox.security import signed_tracking_token

        message = self._message(session, account, outreach_fixture, mailbox)
        response = client.get(
            f"/t/c/{message.id}",
            params={"k": signed_tracking_token(message.id), "u": "https://example.com/job"},
            follow_redirects=False,
        )
        assert response.headers["location"] == "https://example.com/job"

        session.expire_all()
        assert session.get(Message, message.id).clicked_at is not None

    def test_a_non_http_scheme_never_leaves(
        self, client, session, account, outreach_fixture, mailbox
    ):
        from juicebox.security import signed_tracking_token

        message = self._message(session, account, outreach_fixture, mailbox)
        response = client.get(
            f"/t/c/{message.id}",
            params={"k": signed_tracking_token(message.id), "u": "javascript:alert(1)"},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/"
