"""Tags: a join table so the grid can filter on them, plus the vocabulary."""

from __future__ import annotations


def _person(client, headers, name, **extra):
    response = client.post(
        "/v1/candidates",
        json={"full_name": name, "source": "manual", "source_id": name.lower(), **extra},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _set_tags(client, headers, candidate_id, tags):
    return client.patch(
        f"/v1/candidates/{candidate_id}", json={"tags": tags}, headers=headers
    )


class TestSetting:
    def test_tags_replace_the_whole_set(self, client, account):
        candidate_id = _person(client, account["headers"], "Ada")

        assert _set_tags(client, account["headers"], candidate_id, ["warm", "referral"]).json()[
            "tags"
        ] == ["referral", "warm"]
        # A chip editor sends the whole set; sending fewer removes the rest.
        assert _set_tags(client, account["headers"], candidate_id, ["warm"]).json()["tags"] == [
            "warm"
        ]


class TestFiltering:
    def test_the_grid_filters_by_tag(self, client, account):
        tagged = _person(client, account["headers"], "Tagged Person")
        _person(client, account["headers"], "Untagged Person")
        _set_tags(client, account["headers"], tagged, ["warm"])

        rows = client.get(
            "/v1/candidates", params={"tag": "warm"}, headers=account["headers"]
        ).json()
        assert [r["id"] for r in rows] == [tagged]

    def test_the_count_agrees_with_the_tag_filter(self, client, account):
        tagged = _person(client, account["headers"], "Tagged Person")
        _person(client, account["headers"], "Untagged Person")
        _set_tags(client, account["headers"], tagged, ["warm"])

        count = client.get(
            "/v1/candidates/count", params={"tag": "warm"}, headers=account["headers"]
        ).json()["count"]
        assert count == 1


class TestVocabulary:
    def test_it_counts_usage_and_orders_by_it(self, client, account):
        first = _person(client, account["headers"], "First")
        second = _person(client, account["headers"], "Second")
        _set_tags(client, account["headers"], first, ["warm", "referral"])
        _set_tags(client, account["headers"], second, ["warm"])

        rows = client.get("/v1/crm/tags", headers=account["headers"]).json()
        assert rows[0] == {"tag": "warm", "count": 2}
        assert {"tag": "referral", "count": 1} in rows

    def test_a_tag_nobody_uses_stops_being_suggested(self, client, account):
        candidate_id = _person(client, account["headers"], "Ada")
        _set_tags(client, account["headers"], candidate_id, ["temporary"])
        _set_tags(client, account["headers"], candidate_id, [])

        assert client.get("/v1/crm/tags", headers=account["headers"]).json() == []

    def test_the_vocabulary_is_org_scoped(self, client, account):
        candidate_id = _person(client, account["headers"], "Ada")
        _set_tags(client, account["headers"], candidate_id, ["warm"])

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

        assert client.get("/v1/crm/tags", headers=headers).json() == []
