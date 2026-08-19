from __future__ import annotations

from foray.enrich.apollo import _usable
from foray.enrich.emails import accepted_commit_emails, extract_commit_emails
from foray.enrich.employer import domain_of, freemail_domains, is_work_domain


def _push(push_id: str, *commits: tuple[str, str]) -> dict:
    return {
        "type": "PushEvent",
        "id": push_id,
        "payload": {"commits": [{"author": {"name": n, "email": e}} for n, e in commits]},
    }


def test_attribution_guard_accepts_name_match():
    events = [_push("1", ("Jane Doe", "jane@stripe.com"))]
    found = extract_commit_emails(events, candidate_name="Jane Doe", login="janedoe")
    accepted = accepted_commit_emails(found)
    assert accepted == [("jane@stripe.com", 75, "commit author, name match, 1 push(es)")]


def test_attribution_guard_accepts_login_match_without_name():
    events = [_push("1", ("someone", "janedoe@gmail.com"))]
    found = extract_commit_emails(events, candidate_name="Completely Different", login="JaneDoe")
    assert accepted_commit_emails(found)[0][1] == 75


def test_attribution_guard_rejects_single_push_colleague():
    # A merge commit carrying a colleague's address: one push, no name match.
    events = [_push("1", ("Colleague Name", "colleague@stripe.com"))]
    found = extract_commit_emails(events, candidate_name="Jane Doe", login="janedoe")
    assert accepted_commit_emails(found) == []


def test_attribution_guard_accepts_repeat_across_pushes_at_low_confidence():
    events = [
        _push("1", ("nickname", "nick@fastmail.com")),
        _push("2", ("nickname", "nick@fastmail.com")),
    ]
    found = extract_commit_emails(events, candidate_name="Jane Doe", login="janedoe")
    (email, confidence, _evidence) = accepted_commit_emails(found)[0]
    assert (email, confidence) == ("nick@fastmail.com", 55)


def test_noreply_and_bots_filtered():
    events = [
        _push(
            "1",
            ("Jane Doe", "12345+janedoe@users.noreply.github.com"),
            ("dependabot[bot]", "support@dependabot.com"),
            ("gha", "actions@github.com"),
        )
    ]
    found = extract_commit_emails(events, candidate_name="Jane Doe", login="janedoe")
    assert found == {}


def test_apollo_placeholder_rejected():
    assert _usable("email_not_unlocked@stripe.com") is None
    assert _usable("Jane.Doe@Stripe.com ") == "jane.doe@stripe.com"
    assert _usable("") is None
    assert _usable(None) is None


def test_domain_of():
    assert domain_of("Jane.Doe@Stripe.com ") == "stripe.com"
    assert domain_of("not-an-email") is None
    assert domain_of("") is None
    assert domain_of(None) is None  # type: ignore[arg-type]


def test_is_work_domain_uses_freemail_list():
    assert "gmail.com" in freemail_domains()  # sanity: the data file loads
    assert not is_work_domain("someone@gmail.com")
    assert is_work_domain("someone@stripe.com")
    assert not is_work_domain("garbage")
