"""The CRM grid: sorting, relationship filters, and a count that agrees.

The filters here decide who a recruiter sees when they ask "who is going
cold" -- a wrong answer is not a visible bug, it is a person quietly never
contacted again. So each bucket is pinned to an explicit fixture rather than
asserted loosely.
"""

from __future__ import annotations

from datetime import timedelta

from juicebox.models import Message, utcnow


def _candidate(client, headers, name, **extra):
    """Seed through the bulk import path, which is how sourced people arrive.

    Not POST /v1/candidates: adding someone by hand is an explicit CRM action
    and marks them crm_tracked, which would put every fixture in CRM scope and
    leave this file unable to test the pool/CRM distinction at all.
    """
    payload = {"full_name": name, "source": "test", "source_id": name.lower(), **extra}
    response = client.post(
        "/v1/candidates/bulk", json={"candidates": [payload]}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["ids"][0]


def _mail(session, org_id, candidate_id, *, direction, days_ago, status=None):
    session.add(
        Message(
            org_id=org_id,
            enrollment_id=None,
            candidate_id=candidate_id,
            direction=direction,
            source="microsoft",
            status=status or ("sent" if direction == "outbound" else "received"),
            sent_at=utcnow() - timedelta(days=days_ago),
        )
    )


def _ids(response):
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()]


def _grid(client, headers, **params):
    return client.get("/v1/candidates", params=params, headers=headers)


def _count(client, headers, **params):
    response = client.get("/v1/candidates/count", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["count"]


def _population(client, session, account):
    """Five people whose histories cover every relationship bucket."""
    headers = account["headers"]
    org = account["org_id"]
    people = {
        # emailed long ago, no answer -> going cold, and a one-touch
        "cold": _candidate(client, headers, "Cold Cassidy"),
        # emailed long ago, they answered long ago -> still stale (all quiet)
        "quiet": _candidate(client, headers, "Quiet Quinn"),
        # emailed long ago, answered recently -> alive, not stale
        "alive": _candidate(client, headers, "Alive Avery"),
        # emailed three days ago, no answer -> too soon to call a non-reply
        "fresh": _candidate(client, headers, "Fresh Frankie"),
        # never emailed at all
        "never": _candidate(client, headers, "Never Nolan"),
    }

    _mail(session, org, people["cold"], direction="outbound", days_ago=200)

    _mail(session, org, people["quiet"], direction="outbound", days_ago=300)
    _mail(session, org, people["quiet"], direction="outbound", days_ago=290)
    _mail(session, org, people["quiet"], direction="inbound", days_ago=280)

    _mail(session, org, people["alive"], direction="outbound", days_ago=200)
    _mail(session, org, people["alive"], direction="inbound", days_ago=2)

    _mail(session, org, people["fresh"], direction="outbound", days_ago=3)

    session.commit()
    return people


class TestScope:
    def test_crm_scope_excludes_the_untouched_pool(self, client, session, account):
        people = _population(client, session, account)
        rows = _ids(_grid(client, account["headers"], scope="crm", limit=100))

        assert people["never"] not in rows
        assert people["cold"] in rows
        assert len(rows) == 4

    def test_default_scope_is_the_whole_org(self, client, session, account):
        """The sequence editor's picker sends no scope and must keep seeing
        everyone, including people with no mail."""
        people = _population(client, session, account)
        rows = _ids(_grid(client, account["headers"], limit=100))
        assert people["never"] in rows
        assert len(rows) == 5

    def test_response_is_still_a_bare_array(self, client, session, account):
        _population(client, session, account)
        body = _grid(client, account["headers"]).json()
        assert isinstance(body, list)


class TestRelationships:
    def test_going_cold_is_quiet_on_both_sides(self, client, session, account):
        people = _population(client, session, account)
        rows = _ids(
            _grid(client, account["headers"], relationship="stale", stale_days=90, limit=100)
        )

        assert people["cold"] in rows
        # answered us 280 days ago, and nothing since: still cold
        assert people["quiet"] in rows
        # replied two days ago -- the relationship is alive
        assert people["alive"] not in rows
        assert people["fresh"] not in rows
        assert people["never"] not in rows

    def test_no_reply_has_a_grace_period(self, client, session, account):
        people = _population(client, session, account)
        rows = _ids(
            _grid(client, account["headers"], relationship="never_replied", limit=100)
        )

        assert people["cold"] in rows
        # emailed three days ago: they have not failed to reply yet
        assert people["fresh"] not in rows
        assert people["quiet"] not in rows
        assert people["never"] not in rows

    def test_one_touch_is_exactly_one_outbound(self, client, session, account):
        people = _population(client, session, account)
        rows = _ids(_grid(client, account["headers"], relationship="one_touch", limit=100))

        assert rows == [people["cold"]]

    def test_replied_is_anyone_who_answered(self, client, session, account):
        people = _population(client, session, account)
        rows = _ids(_grid(client, account["headers"], relationship="replied", limit=100))

        assert sorted(rows) == sorted([people["quiet"], people["alive"]])

    def test_an_active_enrolment_is_never_offered_for_revival(
        self, client, session, account
    ):
        """They are already being chased. Reviving them would double-touch."""
        from juicebox.models import Enrollment, Sequence

        people = _population(client, session, account)
        sequence = Sequence(org_id=account["org_id"], name="Outreach", status="active")
        session.add(sequence)
        session.flush()
        session.add(
            Enrollment(
                sequence_id=sequence.id,
                candidate_id=people["cold"],
                status="active",
            )
        )
        session.commit()

        rows = _ids(_grid(client, account["headers"], relationship="stale", limit=100))
        assert people["cold"] not in rows

    def test_an_unknown_relationship_is_rejected(self, client, account):
        response = _grid(client, account["headers"], relationship="lukewarm")
        assert response.status_code == 422


class TestSorting:
    def test_never_contacted_sort_last_in_both_directions(
        self, client, session, account
    ):
        """The whole point of NULLS LAST: "most recently contacted" must not
        open with 200k people who were never contacted at all."""
        people = _population(client, session, account)

        for direction in ("asc", "desc"):
            rows = _ids(
                _grid(
                    client,
                    account["headers"],
                    sort="last_contacted",
                    dir=direction,
                    limit=100,
                )
            )
            assert rows[-1] == people["never"], direction

    def test_going_cold_sorts_coldest_first(self, client, session, account):
        people = _population(client, session, account)
        rows = _ids(
            _grid(
                client,
                account["headers"],
                relationship="stale",
                sort="last_contacted",
                dir="asc",
                limit=100,
            )
        )
        # quiet was last emailed 290 days ago, cold 200
        assert rows == [people["quiet"], people["cold"]]

    def test_touches_orders_by_outbound_volume(self, client, session, account):
        people = _population(client, session, account)
        rows = _ids(
            _grid(client, account["headers"], sort="touches", dir="desc", limit=100)
        )
        assert rows[0] == people["quiet"]  # two outbound, more than anyone else

    def test_an_unknown_sort_key_is_rejected(self, client, account):
        assert _grid(client, account["headers"], sort="vibes").status_code == 422
        assert _grid(client, account["headers"], dir="sideways").status_code == 422


class TestCount:
    def test_count_honours_the_same_filters_as_the_listing(
        self, client, session, account
    ):
        """These used to disagree -- /count ignored every filter."""
        _population(client, session, account)

        for params in (
            {},
            {"scope": "crm"},
            {"relationship": "stale", "stale_days": 90},
            {"relationship": "replied"},
            {"q": "Cold"},
        ):
            listed = _ids(_grid(client, account["headers"], limit=500, **params))
            assert _count(client, account["headers"], **params) == len(listed), params


class TestOrgScoping:
    def test_another_org_sees_none_of_it(self, client, session, account):
        _population(client, session, account)
        other = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival",
                "email": "rival@rival-talent.com",
                "password": "supersecret1",
                "name": "Bo",
            },
        )
        headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

        assert _ids(_grid(client, headers, limit=100)) == []
        assert _count(client, headers) == 0
        assert _count(client, headers, relationship="stale") == 0


class TestHistoryAgreement:
    def test_grid_stats_match_the_timeline_aggregate(self, client, session, account):
        """The grid sorts on one aggregate and the row renders another; if they
        ever diverge the UI lies about the number it just sorted by."""
        people = _population(client, session, account)
        rows = _grid(
            client, account["headers"], scope="crm", include_history=True, limit=100
        ).json()
        by_id = {row["id"]: row for row in rows}

        quiet = by_id[people["quiet"]]["history"]
        assert quiet["outbound_count"] == 2
        assert quiet["inbound_count"] == 1

        timeline = client.get(
            f"/v1/candidates/{people['quiet']}/timeline", headers=account["headers"]
        ).json()["stats"]
        assert timeline["outbound_count"] == quiet["outbound_count"]
        assert timeline["inbound_count"] == quiet["inbound_count"]
        assert timeline["last_emailed_at"] == quiet["last_emailed_at"]


class TestUnchangedContract:
    def test_a_candidate_with_no_messages_still_lists(self, client, account):
        """A brand-new org with no mail at all must not 500 on the outer join."""
        _candidate(client, account["headers"], "Solo Sam")
        rows = _ids(_grid(client, account["headers"]))
        assert len(rows) == 1
        assert _count(client, account["headers"]) == 1


class TestSelectAllIds:
    def test_ids_match_the_listing_for_the_same_filters(self, client, session, account):
        people = _population(client, session, account)
        response = client.get(
            "/v1/candidates/ids",
            params={"relationship": "stale"},
            headers=account["headers"],
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert sorted(body["ids"]) == sorted([people["cold"], people["quiet"]])
        assert body["truncated"] is False

    def test_truncated_is_true_when_the_cap_bites(self, client, session, account):
        """The UI must not claim it selected everything when it selected a
        thousand of them."""
        _population(client, session, account)
        response = client.get(
            "/v1/candidates/ids", params={"limit": 2}, headers=account["headers"]
        )
        body = response.json()
        assert len(body["ids"]) == 2
        assert body["truncated"] is True

    def test_ids_is_not_swallowed_by_the_candidate_id_route(self, client, account):
        """/ids is declared above /{candidate_id}; if that ever flips, this
        returns a 404 for a candidate literally named "ids"."""
        response = client.get("/v1/candidates/ids", headers=account["headers"])
        assert response.status_code == 200
        assert "ids" in response.json()

    def test_the_cap_is_enforced_server_side(self, client, account):
        response = client.get(
            "/v1/candidates/ids", params={"limit": 5000}, headers=account["headers"]
        )
        assert response.status_code == 422
