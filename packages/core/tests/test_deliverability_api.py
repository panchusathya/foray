"""The Deliverability API: seeds, the per-domain overview, health labels.

From the failure list (items 15-17, 21): a seed's password never comes back
out, a seed that cannot sign in is refused at the form, young cohorts do not
drag a mailbox's reply rate down, and too little data is "no data", not green.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from juicebox.deliverability import seeds as seeds_module
from juicebox.deliverability.health import mailbox_health
from juicebox.deliverability.seeds import Placement, SeedReadError
from juicebox.deliverability.signals import classify_bounce
from juicebox.models import (
    Candidate,
    Enrollment,
    MailboxConnection,
    Message,
    SeedInbox,
    SeedTest,
    Sequence,
    utcnow,
)


class OkReader:
    spam_visible = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def find(self, probe):
        return Placement(found=False)


class RefusingReader(OkReader):
    def __enter__(self):
        raise SeedReadError("Could not sign in to seed@gmail.com: check the app password")


@pytest.fixture
def readers(monkeypatch):
    holder = {"reader": OkReader(), "seen": []}

    def factory(seed):
        holder["seen"].append(seed)
        return holder["reader"]

    monkeypatch.setattr(seeds_module, "_reader_factory", factory)
    return holder


@pytest.fixture
def no_dns(monkeypatch):
    from juicebox.deliverability import dns_checks

    def resolve(name, rdtype):
        raise dns_checks.NoRecord(name)

    monkeypatch.setattr(dns_checks, "_default_resolve", resolve)


SEED = {
    "address": "seed@gmail.com",
    "provider_label": "gmail",
    "password": "abcd efgh ijkl mnop",
}


def test_an_empty_org(client, account):
    response = client.get("/v1/deliverability", headers=account["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["seeds"] == [] and body["domains"] == []
    assert body["settings"]["seed_test_interval_hours"] == 24


class TestSeeds:
    def test_a_seed_that_cannot_sign_in_is_refused_and_not_saved(
        self, client, account, readers, session
    ):
        readers["reader"] = RefusingReader()
        response = client.post("/v1/deliverability/seeds", json=SEED, headers=account["headers"])
        assert response.status_code == 400
        assert "sign in" in response.json()["detail"]
        assert SEED["password"] not in response.text
        assert session.query(SeedInbox).count() == 0

    def test_a_good_seed_is_saved_and_its_password_never_returned(
        self, client, account, readers, session
    ):
        response = client.post("/v1/deliverability/seeds", json=SEED, headers=account["headers"])
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["address"] == "seed@gmail.com"
        assert body["imap_host"] == "imap.gmail.com"  # the Gmail preset
        assert "password" not in body and "secret_blob" not in body
        stored = session.query(SeedInbox).one()
        assert SEED["password"] not in stored.secret_blob
        overview = client.get("/v1/deliverability", headers=account["headers"]).text
        assert SEED["password"] not in overview and stored.secret_blob not in overview
        # The reader was handed the password the form sent, spaces and all
        # stripped the way Google displays app passwords.
        assert readers["seen"][0].username == "seed@gmail.com"

    def test_the_same_seed_twice_is_a_conflict(self, client, account, readers):
        client.post("/v1/deliverability/seeds", json=SEED, headers=account["headers"])
        again = client.post("/v1/deliverability/seeds", json=SEED, headers=account["headers"])
        assert again.status_code == 409

    def test_other_imap_needs_a_host(self, client, account, readers):
        response = client.post(
            "/v1/deliverability/seeds",
            json={"address": "seed@fastmail.com", "provider_label": "other", "password": "x"},
            headers=account["headers"],
        )
        assert response.status_code == 422

    def test_pause_and_delete(self, client, account, readers, session):
        seed_id = client.post(
            "/v1/deliverability/seeds", json=SEED, headers=account["headers"]
        ).json()["id"]
        paused = client.patch(
            f"/v1/deliverability/seeds/{seed_id}",
            json={"status": "paused"},
            headers=account["headers"],
        )
        assert paused.json()["status"] == "paused"
        assert (
            client.delete(
                f"/v1/deliverability/seeds/{seed_id}", headers=account["headers"]
            ).status_code
            == 204
        )
        assert session.query(SeedInbox).count() == 0


class TestSettings:
    def test_interval_is_saved(self, client, account):
        response = client.patch(
            "/v1/deliverability/settings",
            json={"seed_test_interval_hours": 48},
            headers=account["headers"],
        )
        assert response.status_code == 200
        body = client.get("/v1/deliverability", headers=account["headers"]).json()
        assert body["settings"]["seed_test_interval_hours"] == 48

    def test_only_offered_intervals(self, client, account):
        response = client.patch(
            "/v1/deliverability/settings",
            json={"seed_test_interval_hours": 1},
            headers=account["headers"],
        )
        assert response.status_code == 422


@pytest.fixture
def traffic(session, account, outreach_fixture):
    """Two goforay mailboxes and a consumer Gmail sender, with real-looking history."""
    org_id = account["org_id"]
    now = utcnow()
    ryan = MailboxConnection(org_id=org_id, address="ryan@goforay.io", daily_send_cap=5)
    maya = MailboxConnection(org_id=org_id, address="maya@goforay.io", daily_send_cap=5)
    gmail = MailboxConnection(
        org_id=org_id, address="someone@gmail.com", daily_send_cap=20, provider="gmail"
    )
    session.add_all([ryan, maya, gmail])
    session.flush()
    sequence: Sequence = outreach_fixture["sequence"]
    step = sorted(sequence.steps, key=lambda s: s.step_order)[0]

    def person(n, mailbox, first_sent, *, replied=False):
        candidate = Candidate(
            org_id=org_id,
            source="t",
            source_id=f"p{n}",
            full_name=f"P {n}",
            emails=[f"p{n}@example.com"],
        )
        session.add(candidate)
        session.flush()
        enrollment = Enrollment(
            sequence_id=sequence.id,
            candidate_id=candidate.id,
            mailbox_id=mailbox.id,
            status="replied" if replied else "active",
            stopped_reason="interested" if replied else "",
        )
        session.add(enrollment)
        session.flush()
        session.add(
            Message(
                org_id=org_id,
                enrollment_id=enrollment.id,
                candidate_id=candidate.id,
                step_id=step.id,
                direction="outbound",
                source="sequence",
                status="sent",
                mailbox_id=mailbox.id,
                to_address=f"p{n}@example.com",
                subject="Role",
                sent_at=first_sent,
            )
        )
        return candidate, enrollment

    # ryan: 10 people a week ago, 2 replied; 5 people yesterday, none yet.
    for n in range(10):
        person(n, ryan, now - timedelta(days=7), replied=n < 2)
    for n in range(10, 15):
        person(n, ryan, now - timedelta(days=1))
    # maya: one person, who bounced on a policy block.
    candidate, enrollment = person(20, maya, now - timedelta(days=5))
    session.add(
        Message(
            org_id=org_id,
            enrollment_id=enrollment.id,
            candidate_id=candidate.id,
            direction="inbound",
            source="sequence",
            status="received",
            mailbox_id=maya.id,
            reply_intent="bounce",
            sent_at=now - timedelta(days=5),
            reply_text="550 5.7.1 Message rejected: our system has detected this message as spam",
        )
    )
    session.commit()
    return {"ryan": ryan, "maya": maya, "gmail": gmail, "now": now}


class TestOverview:
    def _overview(self, client, account):
        response = client.get("/v1/deliverability", headers=account["headers"])
        assert response.status_code == 200, response.text
        return response.json()

    def test_mailboxes_are_grouped_by_sending_domain(self, client, account, traffic, no_dns):
        body = self._overview(client, account)
        domains = {d["domain"]: d for d in body["domains"]}
        assert set(domains) == {"goforay.io", "gmail.com"}
        assert {m["address"] for m in domains["goforay.io"]["mailboxes"]} == {
            "ryan@goforay.io",
            "maya@goforay.io",
        }
        assert domains["gmail.com"]["consumer"] is True
        assert domains["gmail.com"]["checks"] is None

    def test_reply_rate_leaves_out_people_emailed_in_the_last_three_days(
        self, client, account, traffic, no_dns
    ):
        body = self._overview(client, account)
        [ryan] = [
            m for d in body["domains"] for m in d["mailboxes"] if m["address"] == "ryan@goforay.io"
        ]
        assert ryan["people_recent"] == 10
        assert ryan["replied_recent"] == 2
        assert ryan["reply_rate_recent"] == pytest.approx(0.2)

    def test_a_policy_bounce_counts_as_a_block(self, client, account, traffic, no_dns):
        body = self._overview(client, account)
        [maya] = [
            m for d in body["domains"] for m in d["mailboxes"] if m["address"] == "maya@goforay.io"
        ]
        assert maya["blocks_14d"] == 1

    def test_seed_results_roll_up_per_mailbox(
        self, client, account, traffic, readers, session, no_dns
    ):
        seed_id = client.post(
            "/v1/deliverability/seeds", json=SEED, headers=account["headers"]
        ).json()["id"]
        now = traffic["now"]
        for n, status in enumerate(("spam", "spam", "spam", "inbox")):
            session.add(
                SeedTest(
                    org_id=account["org_id"],
                    mailbox_id=traffic["ryan"].id,
                    seed_id=seed_id,
                    job_key=f"k-{n}",
                    status=status,
                    sent_at=now - timedelta(days=1),
                    subject="Role",
                )
            )
        session.commit()
        body = self._overview(client, account)
        [ryan] = [
            m for d in body["domains"] for m in d["mailboxes"] if m["address"] == "ryan@goforay.io"
        ]
        assert ryan["seed"]["spam"] == 3 and ryan["seed"]["inbox"] == 1
        assert ryan["health"]["label"] == "in_spam"
        assert body["recent_tests"][0]["mailbox_address"] == "ryan@goforay.io"

    def test_test_now_is_scoped_to_the_org(self, client, account, traffic):
        other = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Other",
                "email": "x@other-org.com",
                "password": "supersecret1",
                "name": "X",
            },
        ).json()
        headers = {"Authorization": f"Bearer {other['access_token']}"}
        response = client.post(
            f"/v1/deliverability/mailboxes/{traffic['ryan'].id}/test", headers=headers
        )
        assert response.status_code == 404


class TestHealthLabels:
    def test_too_little_data_is_not_green(self):
        assert mailbox_health(seed={}, people_recent=5, reply_rate_recent=0.0)["label"] == "no_data"

    def test_mostly_spam_seeds(self):
        result = mailbox_health(seed={"spam": 3, "inbox": 1}, people_recent=0, reply_rate_recent=0)
        assert result["label"] == "in_spam"
        assert result["reasons"]

    def test_policy_blocks(self):
        result = mailbox_health(seed={}, people_recent=100, reply_rate_recent=0.06, blocks=2)
        assert result["label"] == "in_spam"

    def test_reply_rate_well_below_the_domain(self):
        result = mailbox_health(
            seed={}, people_recent=100, reply_rate_recent=0.02, domain_reply_rate=0.07
        )
        assert result["label"] == "at_risk"

    def test_healthy(self):
        result = mailbox_health(
            seed={"inbox": 5, "other_tab": 1},
            people_recent=100,
            reply_rate_recent=0.07,
            domain_reply_rate=0.065,
            bounce_rate=0.01,
        )
        assert result["label"] == "healthy"


class TestBounceKinds:
    @pytest.mark.parametrize(
        "text",
        [
            "550 5.7.1 Message rejected as spam",
            "554 5.7.26 Unauthenticated email is not accepted from this domain",
            "550 5.7.350 Remote server returned message detected as spam",
            "Our system has detected that this message is likely suspicious due to the very low reputation of the sending domain",
        ],
    )
    def test_blocks(self, text):
        assert classify_bounce(text) == "block"

    @pytest.mark.parametrize(
        "text",
        [
            "550 5.1.1 The email account that you tried to reach does not exist",
            "550 5.1.1 <x@yahoo.com>: user unknown",
            "552 5.2.2 mailbox full",
        ],
    )
    def test_address_problems(self, text):
        assert classify_bounce(text) == "address"
