"""Client dashboards: a hiring company's own login onto the people we shared.

The tests that matter most are the walls. A client token must not open any
recruiter route, a recruiter token must not open the client API, and a
client's response must never carry anything off the CRM row.
"""

from __future__ import annotations

import pytest

PASSWORD = "flowgen-test-pw1"


def _signup(client, org_name: str, email: str) -> dict:
    response = client.post(
        "/v1/auth/signup",
        json={"org_name": org_name, "email": email, "password": "supersecret1", "name": "Sam"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _make_client(client, headers, name="Flowgen", username="flowgen") -> dict:
    created = client.post("/v1/clients", json={"name": name}, headers=headers)
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]
    login = client.post(
        f"/v1/clients/{account_id}/logins",
        json={"username": username, "password": PASSWORD},
        headers=headers,
    )
    assert login.status_code == 201, login.text
    token = client.post(
        "/v1/client/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert token.status_code == 200, token.text
    return {
        "account_id": account_id,
        "login_id": login.json()["id"],
        "headers": {"Authorization": f"Bearer {token.json()['access_token']}"},
        "user": token.json()["user"],
    }


def _contact(client, headers, **fields) -> str:
    body = {
        "full_name": "Priya Raman",
        "emails": ["priya@example.com"],
        "phones": ["+14155550123"],
        "linkedin_url": "https://www.linkedin.com/in/priya-raman",
        "current_title": "Staff Engineer",
        "current_company": "Initech",
    }
    body.update(fields)
    response = client.post("/v1/candidates", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture
def recruiter(account):
    return account["headers"]


@pytest.fixture
def flowgen(client, recruiter):
    return _make_client(client, recruiter)


# --------------------------------------------------------------------------
# the walls
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/auth/me",
        "/v1/candidates",
        "/v1/projects",
        "/v1/sequences",
        "/v1/clients",
        "/v1/intake/calls",
        "/v1/applications",
    ],
)
def test_a_client_token_opens_no_recruiter_route(client, flowgen, path):
    response = client.get(path, headers=flowgen["headers"])
    assert response.status_code == 401, (path, response.status_code)


def test_a_recruiter_token_opens_no_client_route(client, recruiter, flowgen):
    assert client.get("/v1/client/me", headers=recruiter).status_code == 401
    assert client.get("/v1/client/submissions", headers=recruiter).status_code == 401


def test_the_client_sees_the_snapshot_and_nothing_off_the_contact(client, recruiter, flowgen):
    candidate_id = _contact(client, recruiter)
    client.post(f"/v1/candidates/{candidate_id}/notes", json={"body": "comp 400k"}, headers=recruiter)
    shared = client.post(
        f"/v1/clients/{flowgen['account_id']}/submissions",
        json={"entries": [{"candidate_id": candidate_id}], "role_title": "Backend"},
        headers=recruiter,
    )
    assert shared.json()["added"] == 1

    rows = client.get("/v1/client/submissions", headers=flowgen["headers"]).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["full_name"] == "Priya Raman"
    assert row["current_company"] == "Initech"
    assert row["stage"] == "awaiting"
    raw = str(rows)
    for leaked in ("priya@example.com", "4155550123", "comp 400k", candidate_id):
        assert leaked not in raw
    assert "candidate_id" not in row

    detail = client.get(f"/v1/client/submissions/{row['id']}", headers=flowgen["headers"])
    assert candidate_id not in detail.text and "priya@example.com" not in detail.text

    # We do get the link back to the contact.
    ours = client.get(f"/v1/clients/{flowgen['account_id']}/submissions", headers=recruiter)
    assert ours.json()[0]["candidate_id"] == candidate_id


def test_another_org_cannot_reach_our_rows(client, recruiter, flowgen):
    shared = client.post(
        f"/v1/clients/{flowgen['account_id']}/submissions",
        json={"entries": [{"full_name": "Ada Lovelace"}]},
        headers=recruiter,
    ).json()
    submission_id = shared["results"][0]["submission_id"]

    other = _signup(client, "Other Firm", "other@acme-talent.com")
    theirs = _make_client(client, other, name="Flowgen", username="flowgen-2")

    assert client.get("/v1/client/submissions", headers=theirs["headers"]).json() == []
    for method, path in [
        ("get", f"/v1/client/submissions/{submission_id}"),
        ("patch", f"/v1/client/submissions/{submission_id}"),
    ]:
        response = getattr(client, method)(
            path, headers=theirs["headers"], **({"json": {"stage": "rejected"}} if method == "patch" else {})
        )
        assert response.status_code == 404
    # The other org's recruiters cannot see it either.
    assert client.get(f"/v1/clients/submissions/{submission_id}", headers=other).status_code == 404
    assert client.get(f"/v1/clients/{flowgen['account_id']}", headers=other).status_code == 404


def test_two_clients_in_one_org_do_not_see_each_other(client, recruiter, flowgen):
    acme = _make_client(client, recruiter, name="Acme Robotics", username="acme")
    client.post(
        f"/v1/clients/{flowgen['account_id']}/submissions",
        json={"entries": [{"full_name": "For Flowgen"}]},
        headers=recruiter,
    )
    assert client.get("/v1/client/submissions", headers=acme["headers"]).json() == []


def test_a_disabled_login_stops_working_at_once(client, recruiter, flowgen):
    client.patch(
        f"/v1/clients/logins/{flowgen['login_id']}", json={"is_active": False}, headers=recruiter
    )
    assert client.get("/v1/client/me", headers=flowgen["headers"]).status_code == 401
    relogin = client.post(
        "/v1/client/auth/login", json={"username": "flowgen", "password": PASSWORD}
    )
    assert relogin.status_code == 403


def test_bad_password_and_unknown_user_look_the_same(client, flowgen):
    wrong = client.post("/v1/client/auth/login", json={"username": "flowgen", "password": "nope"})
    unknown = client.post("/v1/client/auth/login", json={"username": "nobody", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_login_is_case_insensitive_on_the_username(client, flowgen):
    response = client.post(
        "/v1/client/auth/login", json={"username": "  FlowGen ", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["user"]["client_name"] == "Flowgen"


def test_a_username_cannot_look_like_an_email(client, recruiter, flowgen):
    response = client.post(
        f"/v1/clients/{flowgen['account_id']}/logins",
        json={"username": "hiring@flowgen.ai", "password": PASSWORD},
        headers=recruiter,
    )
    assert response.status_code == 422


def test_usernames_are_unique_across_orgs(client, recruiter, flowgen):
    other = _signup(client, "Other Firm", "other@acme-talent.com")
    account_id = client.post("/v1/clients", json={"name": "X"}, headers=other).json()["id"]
    response = client.post(
        f"/v1/clients/{account_id}/logins",
        json={"username": "flowgen", "password": PASSWORD},
        headers=other,
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# sharing
# --------------------------------------------------------------------------


def test_pasting_a_list_twice_updates_rather_than_duplicates(client, recruiter, flowgen):
    candidate_id = _contact(client, recruiter)
    url = f"/v1/clients/{flowgen['account_id']}/submissions"
    first = client.post(
        url,
        json={
            "entries": [
                {"linkedin_url": "linkedin.com/in/priya-raman/"},
                {"linkedin_url": "https://www.linkedin.com/in/jo-bloggs", "stage": "rejected"},
                {"full_name": "Name Only"},
                {"full_name": ""},
            ]
        },
        headers=recruiter,
    ).json()
    assert (first["added"], first["skipped"]) == (3, 1)
    # The URL found the existing contact and took its snapshot.
    assert first["results"][0]["matched_contact"] is True
    assert first["results"][0]["full_name"] == "Priya Raman"
    # An unknown profile still gets a readable name from the slug.
    assert first["results"][1]["full_name"] == "Jo Bloggs"

    second = client.post(
        url,
        json={
            "entries": [
                {"candidate_id": candidate_id, "stage": "first_round"},
                {"linkedin_url": "https://linkedin.com/in/JO-BLOGGS", "stage": "rejected"},
                {"full_name": "name  only"},
            ]
        },
        headers=recruiter,
    ).json()
    assert (second["added"], second["updated"], second["unchanged"]) == (0, 1, 2)

    rows = {r["full_name"]: r for r in client.get(url, headers=recruiter).json()}
    assert len(rows) == 3
    assert rows["Priya Raman"]["stage"] == "first_round"
    assert rows["Jo Bloggs"]["stage"] == "rejected"


def test_sharing_a_project_pipeline(client, recruiter, flowgen):
    project = client.post("/v1/projects", json={"name": "Flowgen backend"}, headers=recruiter)
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    ids = [
        _contact(client, recruiter, full_name=name, emails=[f"{i}@example.com"], linkedin_url="")
        for i, name in enumerate(["A One", "B Two"])
    ]
    add = client.post(
        f"/v1/projects/{project_id}/candidates", json={"candidate_ids": ids}, headers=recruiter
    )
    assert add.status_code in (200, 201), add.text

    result = client.post(
        f"/v1/clients/{flowgen['account_id']}/submissions",
        json={"project_id": project_id},
        headers=recruiter,
    ).json()
    assert result["added"] == 2


def test_an_unknown_stage_is_refused(client, recruiter, flowgen):
    url = f"/v1/clients/{flowgen['account_id']}/submissions"
    response = client.post(
        url, json={"entries": [{"full_name": "X", "stage": "maybe"}]}, headers=recruiter
    )
    assert response.status_code == 422
    row_id = client.post(url, json={"entries": [{"full_name": "X"}]}, headers=recruiter).json()[
        "results"
    ][0]["submission_id"]
    response = client.patch(
        f"/v1/client/submissions/{row_id}", json={"stage": "maybe"}, headers=flowgen["headers"]
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# both sides see the same thing
# --------------------------------------------------------------------------


def _share_one(client, recruiter, flowgen) -> str:
    return client.post(
        f"/v1/clients/{flowgen['account_id']}/submissions",
        json={"entries": [{"full_name": "Ada Lovelace"}]},
        headers=recruiter,
    ).json()["results"][0]["submission_id"]


def test_a_client_stage_move_shows_on_our_side_with_its_author(client, recruiter, flowgen):
    row_id = _share_one(client, recruiter, flowgen)
    moved = client.patch(
        f"/v1/client/submissions/{row_id}",
        json={"stage": "second_round"},
        headers=flowgen["headers"],
    )
    assert moved.status_code == 200
    assert moved.json()["events"][-1]["from_stage"] == "awaiting"

    ours = client.get(f"/v1/clients/submissions/{row_id}", headers=recruiter).json()
    assert ours["stage"] == "second_round"
    assert ours["stage_changed_by_kind"] == "client"
    assert ours["stage_changed_by_name"] == "Flowgen"
    assert [e["to_stage"] for e in ours["events"]] == ["awaiting", "second_round"]

    summary = client.get(f"/v1/clients/{flowgen['account_id']}", headers=recruiter).json()
    assert summary["stage_counts"]["second_round"] == 1
    assert summary["client_activity_7d"] == 1


def test_setting_the_same_stage_records_nothing(client, recruiter, flowgen):
    row_id = _share_one(client, recruiter, flowgen)
    for _ in range(2):
        client.patch(
            f"/v1/client/submissions/{row_id}", json={"stage": "awaiting"}, headers=flowgen["headers"]
        )
    ours = client.get(f"/v1/clients/submissions/{row_id}", headers=recruiter).json()
    assert len(ours["events"]) == 1
    assert ours["stage_changed_by_kind"] == "recruiter"


def test_notes_are_one_shared_thread(client, recruiter, flowgen):
    row_id = _share_one(client, recruiter, flowgen)
    theirs = client.post(
        f"/v1/client/submissions/{row_id}/notes",
        json={"body": "Strong on systems design"},
        headers=flowgen["headers"],
    ).json()
    ours = client.post(
        f"/v1/clients/submissions/{row_id}/notes",
        json={"body": "Available from November"},
        headers=recruiter,
    ).json()
    assert theirs["mine"] is True and ours["mine"] is True

    seen_by_client = client.get(
        f"/v1/client/submissions/{row_id}", headers=flowgen["headers"]
    ).json()
    assert [(n["author_kind"], n["body"], n["mine"]) for n in seen_by_client["notes"]] == [
        ("client", "Strong on systems design", True),
        ("recruiter", "Available from November", False),
    ]
    listing = client.get("/v1/client/submissions", headers=flowgen["headers"]).json()
    assert listing[0]["note_count"] == 2
    assert listing[0]["latest_note"]["body"] == "Available from November"

    # The client cannot remove our note; we can remove theirs.
    assert (
        client.delete(f"/v1/client/notes/{ours['id']}", headers=flowgen["headers"]).status_code
        == 404
    )
    assert client.delete(f"/v1/clients/notes/{theirs['id']}", headers=recruiter).status_code == 204
    assert (
        client.delete(f"/v1/client/notes/{ours['id']}", headers=recruiter).status_code == 401
    )


def test_bulk_stage_moves_only_this_clients_rows(client, recruiter, flowgen):
    acme = _make_client(client, recruiter, name="Acme Robotics", username="acme")
    mine = _share_one(client, recruiter, flowgen)
    theirs = client.post(
        f"/v1/clients/{acme['account_id']}/submissions",
        json={"entries": [{"full_name": "Acme Person"}]},
        headers=recruiter,
    ).json()["results"][0]["submission_id"]
    moved = client.post(
        f"/v1/clients/{flowgen['account_id']}/submissions/stage",
        json={"ids": [mine, theirs], "stage": "rejected"},
        headers=recruiter,
    ).json()
    assert [r["id"] for r in moved] == [mine]
    acme_row = client.get(f"/v1/clients/submissions/{theirs}", headers=recruiter).json()
    assert acme_row["stage"] == "awaiting"


def test_unsharing_takes_the_row_off_the_client_dashboard(client, recruiter, flowgen):
    row_id = _share_one(client, recruiter, flowgen)
    client.post(
        f"/v1/client/submissions/{row_id}/notes", json={"body": "hi"}, headers=flowgen["headers"]
    )
    assert client.delete(f"/v1/clients/submissions/{row_id}", headers=recruiter).status_code == 204
    assert client.get("/v1/client/submissions", headers=flowgen["headers"]).json() == []
