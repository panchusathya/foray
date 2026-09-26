"""The CRM grid: sorting, relationship filters, and a count that agrees.

The filters here decide who a recruiter sees when they ask "who is going
cold" -- a wrong answer is not a visible bug, it is a person quietly never
contacted again. So each bucket is pinned to an explicit fixture rather than
asserted loosely.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

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

    def test_an_active_enrolment_is_only_excluded_when_asked(
        self, client, session, account
    ):
        """This used to be unconditional, and it emptied the view in
        production: an enrollment is ``active`` until the last step sends or
        the person answers, so on a multi-step sequence the people who have
        not replied yet are exactly the ones still active."""
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

        # Visible by default: being mid-sequence is context, not a reason to
        # disappear from a view whose whole job is finding people.
        assert people["cold"] in _ids(
            _grid(client, account["headers"], relationship="stale", limit=100)
        )
        # ...and still filterable for anyone who wants only the unattended.
        assert people["cold"] not in _ids(
            _grid(
                client,
                account["headers"],
                relationship="stale",
                exclude_in_sequence="true",
                limit=100,
            )
        )


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


class TestMidSequencePeopleAreNotHidden:
    """Reported from production: "No reply" claimed everyone had replied.

    An enrollment stays ``active`` from the moment someone is enrolled until
    the last step sends or they answer, so on any multi-step sequence the
    people who have not replied *yet* are exactly the ones still active. Hiding
    them emptied the view that exists to find them.
    """

    def _mid_sequence(self, client, session, account, *, days_ago, name):
        from juicebox.models import Enrollment, Sequence

        candidate = _candidate(client, account["headers"], name)
        _mail(session, account["org_id"], candidate, direction="outbound", days_ago=days_ago)
        sequence = session.scalar(select(Sequence)) or Sequence(
            org_id=account["org_id"], name="Outreach", status="active"
        )
        session.add(sequence)
        session.flush()
        session.add(
            Enrollment(
                sequence_id=sequence.id, candidate_id=candidate, status="active"
            )
        )
        session.commit()
        return candidate

    def test_no_reply_shows_someone_still_mid_sequence(
        self, client, session, account
    ):
        candidate = self._mid_sequence(
            client, session, account, days_ago=30, name="Waiting Wren"
        )

        rows = _ids(
            _grid(client, account["headers"], relationship="never_replied", limit=100)
        )
        assert candidate in rows

    def test_going_cold_shows_a_stalled_sequence(self, client, session, account):
        """An enrollment that has been active and silent for 200 days is a
        stalled sequence, which is precisely worth seeing."""
        candidate = self._mid_sequence(
            client, session, account, days_ago=200, name="Stalled Sam"
        )

        rows = _ids(_grid(client, account["headers"], relationship="stale", limit=100))
        assert candidate in rows

    def test_the_grace_period_still_applies(self, client, session, account):
        """Fixing the enrolment bug must not also drop the 7-day floor."""
        candidate = self._mid_sequence(
            client, session, account, days_ago=2, name="Fresh Fern"
        )

        rows = _ids(
            _grid(client, account["headers"], relationship="never_replied", limit=100)
        )
        assert candidate not in rows


class TestProjectScope:
    """The grid, narrowed to one mandate.

    "Who replied to the robot role?" is a question about a project, and the
    answer has to include people the project only ever emailed -- enrolling
    somebody does not put them on the board, so a pipeline-only filter would
    drop the repliers this view exists to find.
    """

    def _project(self, client, headers, name="Robot Role"):
        response = client.post("/v1/projects", json={"name": name}, headers=headers)
        assert response.status_code in (200, 201), response.text
        return response.json()["id"]

    def _enroll(self, session, org_id, candidate_id, project_id):
        from juicebox.models import Enrollment, Sequence

        sequence = Sequence(org_id=org_id, name="Robot outreach", project_id=project_id)
        session.add(sequence)
        session.flush()
        session.add(
            Enrollment(
                sequence_id=sequence.id,
                candidate_id=candidate_id,
                project_id=project_id,
                status="active",
            )
        )

    def test_includes_people_the_project_only_emailed(self, client, session, account):
        """Enrolment creates no pipeline row, so this is the case a
        membership-only filter gets wrong."""
        people = _population(client, session, account)
        project = self._project(client, account["headers"])
        self._enroll(session, account["org_id"], people["quiet"], project)
        session.commit()

        rows = _ids(_grid(client, account["headers"], project_id=project, limit=100))
        assert rows == [people["quiet"]]

    def test_composes_with_the_relationship_filters(self, client, session, account):
        """The actual question: who replied to *this* project's outreach."""
        people = _population(client, session, account)
        project = self._project(client, account["headers"])
        # "quiet" answered 280 days ago; "cold" never did.
        self._enroll(session, account["org_id"], people["quiet"], project)
        self._enroll(session, account["org_id"], people["cold"], project)
        session.commit()

        rows = _ids(
            _grid(
                client,
                account["headers"],
                project_id=project,
                scope="crm",
                relationship="replied",
                limit=100,
            )
        )
        assert rows == [people["quiet"]]

    def test_the_count_agrees_with_the_listing(self, client, session, account):
        people = _population(client, session, account)
        project = self._project(client, account["headers"])
        self._enroll(session, account["org_id"], people["cold"], project)
        session.commit()

        params = {"project_id": project, "scope": "crm"}
        assert _count(client, account["headers"], **params) == len(
            _ids(_grid(client, account["headers"], limit=100, **params))
        )

    def test_an_unknown_project_matches_nobody(self, client, session, account):
        """A deleted or foreign project id is empty, never unfiltered."""
        _population(client, session, account)
        rows = _ids(_grid(client, account["headers"], project_id="proj_nope", limit=100))
        assert rows == []
