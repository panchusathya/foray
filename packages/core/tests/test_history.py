"""Contact-history aggregates and the timeline endpoint."""

from __future__ import annotations

from datetime import timedelta

from juicebox.models import Message, utcnow
from juicebox.outreach.history import candidate_history_stats


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


class TestTimelineApi:

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
