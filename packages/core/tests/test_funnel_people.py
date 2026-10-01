"""The outreach funnel counts people reached, not emails sent."""

from __future__ import annotations

from juicebox.models import Message, utcnow
from juicebox.outreach.sequencer import enroll_candidates


def _send(session, enrollment, *, status="sent", opened=False, replied=False, intent=""):
    now = utcnow()
    session.add(
        Message(
            org_id=enrollment.sequence.org_id,
            enrollment_id=enrollment.id,
            candidate_id=enrollment.candidate_id,
            direction="outbound",
            status=status,
            sent_at=now,
            opened_at=now if opened else None,
            replied_at=now if replied else None,
            reply_intent=intent,
        )
    )


def test_three_steps_to_one_person_is_one_person_contacted(
    client, account, session, outreach_fixture
):
    sequence = outreach_fixture["sequence"]
    ada, bo, cy = outreach_fixture["candidates"][:3]
    one, two, three = enroll_candidates(
        session, sequence=sequence, candidate_ids=[ada.id, bo.id, cy.id]
    )
    # Ada: three steps, opened two, replied interested on the last.
    _send(session, one, opened=True)
    _send(session, one, opened=True)
    _send(session, one, replied=True, intent="interested")
    # Bo: one email, never opened.
    _send(session, two)
    # Cy: bounced. The reply path stamps replied_at on a bounce; not a reply.
    _send(session, three, status="bounced", replied=True)
    # A manual email the mailbox sync found in a sent folder: no sequence.
    session.add(
        Message(
            org_id=sequence.org_id,
            candidate_id=bo.id,
            direction="outbound",
            status="sent",
            sent_at=utcnow(),
        )
    )
    session.commit()

    body = client.get("/v1/analytics/funnel", headers=account["headers"]).json()

    assert body["outreach"]["sent"] == 4  # emails: Ada's three and Bo's one
    assert body["people"] == {
        "contacted": 2,
        "opened": 1,
        "clicked": 0,
        "replied": 1,
        "bounced": 1,
        "interested": 1,
    }
    assert body["people_reply_rate"] == 0.5
    assert body["people_open_rate"] == 0.5
    assert body["outreach"]["replied"] == 1  # the bounce is not a reply
