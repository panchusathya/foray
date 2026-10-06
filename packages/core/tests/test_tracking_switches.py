"""Per-sequence Track opens / Track clicks switches."""

from __future__ import annotations

from juicebox.models import Sequence, SequenceStep
from juicebox.outreach import templates
from juicebox.outreach.sequencer import enroll_candidates, run_due

BODY = '<p>Hi {{ first_name }}, details <a href="https://example.com/role">here</a>.</p>'


def _link_step(session, sequence):
    step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
    step.body_template = BODY
    session.commit()


def _send_one(session, fixture, mailbox):
    sequence = fixture["sequence"]
    enroll_candidates(
        session,
        sequence=sequence,
        candidate_ids=[fixture["candidates"][0].id],
        project_id=sequence.project_id,
        send_now=True,
    )
    session.commit()
    run_due(session, org_id=sequence.org_id)
    session.commit()
    [email] = mailbox.outbox
    return email.html_body


class TestAddTracking:
    def test_both_on_is_unchanged(self):
        html = templates.add_tracking(
            '<a href="https://x.com">x</a>', base_url="https://t.io", message_id="m", token="k"
        )
        assert "https://t.io/t/c/m" in html and "/t/o/m.png" in html

    def test_off_leaves_the_html_as_written(self):
        source = '<a href="https://x.com">x</a>'
        html = templates.add_tracking(
            source, base_url="https://t.io", message_id="m", token="k", opens=False, clicks=False
        )
        assert html == source


class TestTheSendPath:
    def test_untouched_sends_no_pixel_and_the_real_link(self, session, outreach_fixture, mailbox):
        """Off by default: these go out from a person's own mailbox, and the
        pixel and the redirect are what make them read as bulk mail."""
        _link_step(session, outreach_fixture["sequence"])

        html = _send_one(session, outreach_fixture, mailbox)

        assert "/t/o/" not in html and "<img" not in html
        assert "/t/c/" not in html
        assert 'href="https://example.com/role"' in html

    def test_switched_on_tracks(self, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        _link_step(session, sequence)
        sequence.track_opens = True
        sequence.track_clicks = True
        session.commit()

        html = _send_one(session, outreach_fixture, mailbox)

        assert "/t/o/" in html
        assert "/t/c/" in html


class TestTheApi:
    def test_patch_and_clone_carry_the_switches(self, client, account, session, outreach_fixture):
        sequence_id = outreach_fixture["sequence"].id
        headers = account["headers"]
        response = client.patch(
            f"/v1/sequences/{sequence_id}",
            json={"track_opens": True, "track_clicks": True},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["track_opens"] is True

        clone = client.post(f"/v1/sequences/{sequence_id}/clone", json={}, headers=headers)
        assert clone.status_code == 201, clone.text
        assert clone.json()["track_opens"] is True
        assert clone.json()["track_clicks"] is True

    def test_new_sequences_do_not_track(self, client, account, session, outreach_fixture):
        sequence = Sequence(org_id=outreach_fixture["sequence"].org_id, name="x")
        session.add(sequence)
        session.add(SequenceStep(sequence=sequence, step_order=0, body_template="hi"))
        session.commit()
        assert sequence.track_opens is False and sequence.track_clicks is False

        created = client.post(
            "/v1/sequences",
            json={"name": "y", "steps": [{"subject_template": "Hi", "body_template": "hi"}]},
            headers=account["headers"],
        )
        assert created.status_code == 201, created.text
        assert created.json()["track_opens"] is False
        assert created.json()["track_clicks"] is False

    def test_the_spam_score_follows_the_switches(self, client, account):
        step = {"subject_template": "Role", "body_template": BODY}
        on = client.post(
            "/v1/sequences/spam-check",
            json={"steps": [step], "track_opens": True, "track_clicks": True},
            headers=account["headers"],
        ).json()["results"][0]
        off = client.post(
            "/v1/sequences/spam-check", json={"steps": [step]}, headers=account["headers"]
        ).json()["results"][0]

        assert on["delivery_score"] > 0
        assert off["delivery_score"] == 0
        assert off["score"] < on["score"]
