"""The Outbox can show one sequence, several, or all of them."""

from __future__ import annotations

from juicebox.models import Sequence, SequenceStep, utcnow
from juicebox.outreach.sequencer import enroll_candidates


def _three_sequences(session, fixture):
    first = fixture["sequence"]
    made = [first]
    for name in ("Second", "Third"):
        sequence = Sequence(org_id=first.org_id, name=name)
        session.add(sequence)
        session.add(SequenceStep(sequence=sequence, step_order=0, body_template="hi"))
        made.append(sequence)
    session.commit()
    now = utcnow()
    for sequence, candidate in zip(made, fixture["candidates"], strict=False):
        [row] = enroll_candidates(session, sequence=sequence, candidate_ids=[candidate.id])
        row.next_send_at = now
    session.commit()
    return made


def _sequences_in(response):
    assert response.status_code == 200, response.text
    return {row["sequence_id"] for row in response.json()["rows"]}


def test_several_sequences_at_once(client, account, session, outreach_fixture):
    a, _b, c = _three_sequences(session, outreach_fixture)

    response = client.get(
        f"/v1/outreach/in-flight?sequence_id={a.id}&sequence_id={c.id}",
        headers=account["headers"],
    )

    assert _sequences_in(response) == {a.id, c.id}


def test_one_sequence_still_works(client, account, session, outreach_fixture):
    _a, b, _c = _three_sequences(session, outreach_fixture)

    response = client.get(f"/v1/outreach/in-flight?sequence_id={b.id}", headers=account["headers"])

    assert _sequences_in(response) == {b.id}


def test_no_filter_shows_every_sequence(client, account, session, outreach_fixture):
    made = _three_sequences(session, outreach_fixture)

    response = client.get("/v1/outreach/in-flight", headers=account["headers"])

    assert _sequences_in(response) == {s.id for s in made}
