"""Contact-history aggregates and the timeline endpoint."""

from __future__ import annotations

from datetime import timedelta

from juicebox.models import Message, utcnow
from juicebox.outreach.history import candidate_history_stats, candidate_timeline


def _message(org_id, candidate_id, *, direction, status, sent_at=None, **extra):
    return Message(
        org_id=org_id,
        enrollment_id=None,
        candidate_id=candidate_id,
        direction=direction,
        source="outlook",
        status=status,
        sent_at=sent_at,
        **extra,
    )


class TestStats:
    def test_first_last_and_counts(self, session, account, loaded_org):
        from juicebox.models import Candidate

        candidate = session.query(Candidate).first()
        now = utcnow()
        for offset, direction, status in [
            (10, "outbound", "sent"),
            (5, "outbound", "sent"),
            (3, "inbound", "received"),
            (1, "outbound", "failed"),  # never reached anyone: excluded
            (0, "outbound", "queued"),  # an intention, not a contact: excluded
        ]:
            session.add(
                _message(
                    account["org_id"],
                    candidate.id,
                    direction=direction,
                    status=status,
                    sent_at=now - timedelta(days=offset),
                )
            )
        session.commit()

        stats = candidate_history_stats(
            session, org_id=account["org_id"], candidate_ids=[candidate.id]
        )[candidate.id]
        assert stats.outbound_count == 2
        assert stats.inbound_count == 1
        assert stats.first_contacted_at.date() == (now - timedelta(days=10)).date()
        assert stats.last_emailed_at.date() == (now - timedelta(days=5)).date()
        assert stats.last_reply_at.date() == (now - timedelta(days=3)).date()

    def test_a_whole_page_of_ids_is_one_call_and_uncontacted_are_absent(
        self, session, account, loaded_org
    ):
        from juicebox.models import Candidate

        candidates = session.query(Candidate).all()
        assert len(candidates) >= 2
        session.add(
            _message(
                account["org_id"],
                candidates[0].id,
                direction="outbound",
                status="sent",
                sent_at=utcnow(),
            )
        )
        session.commit()

        stats = candidate_history_stats(
            session, org_id=account["org_id"], candidate_ids=[c.id for c in candidates]
        )
        assert candidates[0].id in stats
        assert candidates[1].id not in stats  # never contacted -> no row, not zeros

    def test_empty_id_list_short_circuits(self, session, account):
        assert candidate_history_stats(session, org_id=account["org_id"], candidate_ids=[]) == {}


class TestTimeline:
    def test_threads_group_by_conversation_and_sort_newest_first(
        self, session, account, loaded_org
    ):
        from juicebox.models import Candidate

        candidate = session.query(Candidate).first()
        now = utcnow()
        session.add(
            _message(
                account["org_id"], candidate.id,
                direction="outbound", status="sent",
                sent_at=now - timedelta(days=9),
                conversation_id="conv-old", subject="Old thread",
            )
        )
        session.add(
            _message(
                account["org_id"], candidate.id,
                direction="outbound", status="sent",
                sent_at=now - timedelta(days=2),
                conversation_id="conv-new", subject="New thread",
            )
        )
        session.add(
            _message(
                account["org_id"], candidate.id,
                direction="inbound", status="received",
                sent_at=now - timedelta(days=1),
                conversation_id="conv-new", subject="Re: New thread",
            )
        )
        session.commit()

        stats, threads = candidate_timeline(
            session, org_id=account["org_id"], candidate_id=candidate.id
        )
        assert [t.subject for t in threads] == ["Re: New thread", "Old thread"]
        newest = threads[0]
        assert [m.direction for m in newest.messages] == ["outbound", "inbound"]
        assert stats.outbound_count == 2 and stats.inbound_count == 1


class TestTimelineApi:
    def test_endpoint_returns_stats_and_threads(self, client, loaded_org):
        headers = loaded_org["headers"]
        candidate_id = client.get("/v1/candidates?q=ada", headers=headers).json()[0]["id"]
        timeline = client.get(
            f"/v1/candidates/{candidate_id}/timeline", headers=headers
        ).json()
        assert timeline["stats"]["outbound_count"] == 0
        assert timeline["threads"] == []

    def test_cross_org_timeline_is_a_404(self, client, loaded_org):
        headers = loaded_org["headers"]
        candidate_id = client.get("/v1/candidates?q=ada", headers=headers).json()[0]["id"]
        rival = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival",
                "email": "rival-timeline@rival-talent.com",
                "password": "supersecret1",
            },
        ).json()
        response = client.get(
            f"/v1/candidates/{candidate_id}/timeline",
            headers={"Authorization": f"Bearer {rival['access_token']}"},
        )
        assert response.status_code == 404

    def test_list_candidates_include_history_attaches_stats(self, client, loaded_org, mailbox):
        headers = loaded_org["headers"]
        # Reach "contacted" through the real path: enroll + run a sequence.
        sequence = client.post(
            "/v1/sequences",
            json={
                "name": "History seq",
                "personalize": False,
                "steps": [
                    {
                        "delay_days": 0,
                        "subject_template": "Hi {{first_name}}",
                        "body_template": "Hello {{first_name}}",
                    }
                ],
            },
            headers=headers,
        ).json()
        candidate_id = client.get("/v1/candidates?q=ada", headers=headers).json()[0]["id"]
        client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate_id]},
            headers=headers,
        )
        client.post(f"/v1/sequences/{sequence['id']}/run", headers=headers)

        listing = client.get(
            "/v1/candidates?q=ada&include_history=true", headers=headers
        ).json()
        history = listing[0]["history"]
        assert history["outbound_count"] == 1
        assert history["last_emailed_at"] is not None

        plain = client.get("/v1/candidates?q=ada", headers=headers).json()
        assert plain[0]["history"] is None  # not requested -> not computed
