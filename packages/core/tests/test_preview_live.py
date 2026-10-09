"""The editor's live preview: unsaved text, a chosen mailbox, and model-free refreshes.

Written from its failure list:

1. Previewing unsaved text must not save it. The preview route commits on the
   way out, so a step edited in place would be persisted by looking at it.
2. A mailbox from another org must be refused, not rendered -- its name would
   leak into the preview.
3. A ``fast`` refresh must make no model call: the editor fires one after every
   pause in typing.
4. Choosing a mailbox must change the sign-off, which is the reason to choose one.
"""

from __future__ import annotations

import pytest

from juicebox.integrations.crypto import encrypt_json
from juicebox.llm.client import FakeTransport, LLMClient, set_llm
from juicebox.models import MailboxConnection, SequenceStep
from juicebox.outreach.personalize import reset_fragment_cache


def _mailbox(org_id, address, name):
    return MailboxConnection(
        org_id=org_id,
        provider="zoho",
        address=address,
        display_name=name,
        token_blob=encrypt_json({"access_token": "a", "refresh_token": "r", "expires_at": 2**40}),
        sync_state={},
    )


@pytest.fixture
def seq(session, outreach_fixture):
    sequence = outreach_fixture["sequence"]
    step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
    step.subject_template = "Saved subject for {{ first_name }}"
    step.body_template = "Saved body.\n\nBest,\n{{ sender_first_name }}"
    session.commit()
    ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
    return sequence, step, ada


def _preview(client, account, sequence_id, **payload):
    response = client.post(
        f"/v1/sequences/{sequence_id}/preview", json=payload, headers=account["headers"]
    )
    return response


class TestUnsavedText:
    def test_draft_text_is_rendered_but_never_saved(self, client, account, session, seq):
        """Failure 1."""
        sequence, step, ada = seq
        response = _preview(
            client,
            account,
            sequence.id,
            candidate_id=ada.id,
            step_order=0,
            fast=True,
            subject_template="Draft subject for {{ first_name }}",
            body_template="Draft body only.",
        )
        assert response.status_code == 200
        assert response.json()["subject"] == "Draft subject for Ada"
        assert "Draft body only." in response.json()["body"]
        session.expire_all()
        saved = session.get(SequenceStep, step.id)
        assert saved.subject_template == "Saved subject for {{ first_name }}"
        assert saved.body_template.startswith("Saved body.")

    def test_no_draft_renders_the_saved_step(self, client, account, seq):
        sequence, _, ada = seq
        body = _preview(client, account, sequence.id, candidate_id=ada.id, fast=True).json()
        assert body["subject"] == "Saved subject for Ada"


class TestSendAs:
    def test_the_chosen_mailbox_signs_the_email(self, client, account, session, seq):
        """Failure 4: two mailboxes, two sign-offs, and the response says who."""
        sequence, _, ada = seq
        aria = _mailbox(sequence.org_id, "aria@goforay.io", "Aria Chen")
        luna = _mailbox(sequence.org_id, "luna@goforay.io", "Luna Park")
        session.add_all([aria, luna])
        session.commit()
        for box, first in ((aria, "Aria"), (luna, "Luna")):
            body = _preview(
                client, account, sequence.id, candidate_id=ada.id, mailbox_id=box.id, fast=True
            ).json()
            assert body["body"].rstrip().endswith(first)
            assert body["sender_email"] == box.address

    def test_a_mailbox_from_another_org_is_refused(self, client, account, session, seq):
        """Failure 2."""
        sequence, _, ada = seq
        from juicebox.models import Org

        other = Org(name="Elsewhere", slug="elsewhere-preview")
        session.add(other)
        session.flush()
        foreign = _mailbox(other.id, "spy@elsewhere.com", "Someone Else")
        session.add(foreign)
        session.commit()
        response = _preview(
            client, account, sequence.id, candidate_id=ada.id, mailbox_id=foreign.id, fast=True
        )
        assert response.status_code == 404
        assert "Someone" not in response.text


class TestFast:
    @pytest.fixture
    def counting_llm(self):
        reset_fragment_cache()
        transport = FakeTransport(default={"fragments": [{"key": "k", "text": "Model text."}]})
        set_llm(LLMClient(transport=transport, enabled=True))
        yield transport
        set_llm(None)

    def test_a_fast_refresh_makes_no_model_call(self, client, account, session, seq, counting_llm):
        """Failure 3: directives become markers and the model is never asked."""
        sequence, _, ada = seq
        sequence.personalize = True
        session.commit()
        body = _preview(
            client,
            account,
            sequence.id,
            candidate_id=ada.id,
            fast=True,
            body_template="Hi {{ first_name }}, {{ai: one line about their current role}}\n\n{{ opening_line }}",
        ).json()
        assert counting_llm.calls == []
        assert body["ai_status"] == "skipped"
        assert "[AI:" in body["body"]

    def test_without_fast_the_same_step_is_written(
        self, client, account, session, seq, counting_llm
    ):
        sequence, _, ada = seq
        body = _preview(
            client,
            account,
            sequence.id,
            candidate_id=ada.id,
            body_template="Hi {{ first_name }}, {{ai: one line about their current role}}",
        ).json()
        assert counting_llm.calls
        assert body["ai_status"] in {"resolved", "unavailable"}
