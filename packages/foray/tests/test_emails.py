from __future__ import annotations

from foray.enrich.apollo import _usable
from foray.enrich.emails import extract_own_commit_emails


def _commit(email: str) -> dict:
    return {"commit": {"author": {"name": "whoever", "email": email}}}


def test_noreply_and_bots_filtered():
    commits = [
        _commit("12345+janedoe@users.noreply.github.com"),
        _commit("noreply@github.com"),
        _commit("actions@github.com"),
        _commit("dependabot@example.com"),
        _commit("not-an-email"),
    ]
    assert extract_own_commit_emails(commits) == {}


def test_apollo_placeholder_rejected():
    assert _usable("email_not_unlocked@stripe.com") is None
    assert _usable("Jane.Doe@Stripe.com ") == "jane.doe@stripe.com"
    assert _usable("") is None
    assert _usable(None) is None


class StubConn:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)
        self.executed: list[tuple] = []

    async def fetch(self, *_args):
        return self.rows

    async def execute(self, *args):
        self.executed.append(args)

    def statements(self, needle: str) -> list[tuple]:
        return [a for a in self.executed if needle in a[0]]


class TestTheNegativeCacheIsOnlyStampedAfterALookup:
    """``commit_emails_checked_at`` means "we asked GitHub and there was
    nothing", and the 180-day cursor trusts it. A candidate harvested before
    enrich.profiles lands their repos has no repos to ask about, so nothing was
    asked -- stamping there hid them for six months, including from the run
    right after their repos arrived. Same class as the GitHub-429 stamp two
    lines below it, which was already fixed."""

    async def test_a_candidate_with_no_repos_yet_is_not_stamped(self, monkeypatch):
        from foray.config import settings
        from foray.enrich.emails import mine_commit_emails

        monkeypatch.setattr(settings, "github_token", "gh-test")
        conn = StubConn([{"id": 1, "login": "ada", "top_repos": []}])
        stored = await mine_commit_emails(conn)
        assert stored == 0
        assert conn.statements("commit_emails_checked_at") == []
        assert conn.statements("enrich_errors") == []


class TestApolloWillNotCallAFreemailDomainAnEmployer:
    async def test_gmail_is_not_written_as_the_employer_domain(self, monkeypatch):
        from foray.enrich import apollo

        async def _match(_node):
            return {
                "organization": {"primary_domain": "gmail.com"},
                "email": "jane.doe@gmail.com",
                "personal_emails": [],
                "linkedin_url": "https://li/in/jane",
            }

        monkeypatch.setattr(apollo, "people_match", _match)
        conn = StubConn()
        await apollo.match_and_store(conn, 1, {"name": "Jane"})
        assert conn.statements("employer_domain") == []

    async def test_a_personal_address_is_not_promoted_to_work_by_it(self, monkeypatch):
        """The domain is also what decides is_work, so guarding only the column
        write would still file somebody's own gmail as apollo_work at 85 -- and
        outreach reads work/personal off that."""
        from foray.enrich import apollo

        async def _match(_node):
            return {
                "organization": {"primary_domain": "gmail.com"},
                "email": "jane.doe@gmail.com",
                "personal_emails": [],
            }

        monkeypatch.setattr(apollo, "people_match", _match)
        conn = StubConn()
        await apollo.match_and_store(conn, 1, {"name": "Jane"})
        [insert] = conn.statements("insert into candidate_emails")
        assert insert[3] == "apollo_personal"
        assert insert[4] is False and insert[5] == 70
