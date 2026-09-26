"""POST /v1/imports/known -- which rows of an import are already contacts.

The gap this closes: ``upsert_candidates`` dedupes on ``(org_id, source,
source_id)``, so re-uploading the same export merges, but the *same human*
under a different source is invisible to it and lands as a second row. These
tests are mostly about that cross-source case.
"""

from __future__ import annotations


def _seed(client, headers, candidates):
    response = client.post(
        "/v1/candidates/bulk", json={"candidates": candidates}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def _check(client, headers, people):
    response = client.post("/v1/imports/known", json={"people": people}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _by_key(body):
    return {match["key"]: match for match in body["matches"]}


class TestEmailMatch:
    def test_matching_is_case_and_whitespace_insensitive(self, client, loaded_org):
        headers = loaded_org["headers"]
        body = _check(client, headers, [{"key": "0", "emails": ["  ADA@Example.COM "]}])
        assert _by_key(body)["0"]["matched_on"] == "email"

    def test_a_second_address_on_the_row_still_matches(self, client, loaded_org):
        # emails[0] is the personal address the sequencer would mail; a work
        # address further down the list identifies the same person just as well.
        headers = loaded_org["headers"]
        body = _check(
            client,
            headers,
            [{"key": "0", "emails": ["ada@newjob.com", "ada@example.com"]}],
        )
        assert _by_key(body)["0"]["matched_on"] == "email"


class TestLinkedInMatch:
    def test_the_slug_is_folded_server_side(self, client, account):
        # enrich.linkedin_url.linkedin_slug rstrips "._-"; the browser's
        # linkedinSlug does not. Folding on the server is what keeps the two
        # from silently disagreeing, so a trailing dash must still match.
        headers = account["headers"]
        _seed(
            client,
            headers,
            [
                {
                    "source": "manual",
                    "source_id": "man-1",
                    "full_name": "Grace Hopper",
                    "linkedin_url": "https://www.linkedin.com/in/grace-hopper",
                }
            ],
        )
        body = _check(
            client,
            headers,
            [{"key": "0", "linkedin_url": "linkedin.com/in/grace-hopper-"}],
        )
        assert _by_key(body)["0"]["matched_on"] == "linkedin"

    def test_a_non_profile_url_matches_nobody(self, client, account):
        headers = account["headers"]
        _seed(
            client,
            headers,
            [
                {
                    "source": "manual",
                    "source_id": "man-1",
                    "full_name": "Grace Hopper",
                    "linkedin_url": "https://example.com/team",
                }
            ],
        )
        body = _check(client, headers, [{"key": "0", "linkedin_url": "https://example.com/team"}])
        assert body["matches"] == []


class TestNameAndCompanyMatch:
    def test_the_same_name_at_the_same_company_is_matched(self, client, account):
        headers = account["headers"]
        _seed(
            client,
            headers,
            [
                {
                    "source": "manual",
                    "source_id": "man-1",
                    "full_name": "Katherine Johnson",
                    "experiences": [
                        {"company": "Stripe", "title": "Staff Engineer", "is_current": True}
                    ],
                }
            ],
        )
        body = _check(
            client,
            headers,
            [
                {"key": "0", "full_name": "katherine  johnson", "company": "Stripe, Inc."},
                {"key": "1", "full_name": "Katherine Johnson", "company": "Ramp"},
            ],
        )
        matches = _by_key(body)
        assert set(matches) == {"0"}
        assert matches["0"]["matched_on"] == "name_and_company"
        assert "stripe" in matches["0"]["reason"]

    def test_a_one_token_name_never_matches(self, client, account):
        # Every "Info", "Support" and first-name-only contact would otherwise
        # collide with each other. Same _is_person_name gate suggestions uses.
        headers = account["headers"]
        _seed(
            client,
            headers,
            [
                {
                    "source": "manual",
                    "source_id": "man-1",
                    "full_name": "Support",
                    "experiences": [{"company": "Stripe", "title": "n/a", "is_current": True}],
                }
            ],
        )
        body = _check(client, headers, [{"key": "0", "full_name": "Support", "company": "Stripe"}])
        assert body["matches"] == []

    def test_a_name_with_no_company_never_matches(self, client, loaded_org):
        headers = loaded_org["headers"]
        body = _check(client, headers, [{"key": "0", "full_name": "Ada Okonkwo"}])
        assert body["matches"] == []


class TestPrecedenceAndScope:
    def test_another_orgs_contacts_are_invisible(self, client, loaded_org):
        rival = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival",
                "email": "rival-known@rival-talent.com",
                "password": "supersecret1",
            },
        ).json()
        rival_headers = {"Authorization": f"Bearer {rival['access_token']}"}
        body = _check(client, rival_headers, [{"key": "0", "emails": ["ada@example.com"]}])
        assert body["matches"] == []

    def test_the_route_writes_nothing(self, client, loaded_org):
        # It is read-only by design -- no CommittingRoute, no audit row. If it
        # ever started writing, the contact count would move.
        headers = loaded_org["headers"]
        before = client.get("/v1/candidates/count", headers=headers).json()
        _check(client, headers, [{"key": "0", "emails": ["brand-new@example.com"]}])
        after = client.get("/v1/candidates/count", headers=headers).json()
        assert before == after

    def test_the_linkedin_scan_cap_is_reported_not_hidden(self, client, account, monkeypatch):
        # No indexed slug column, so LinkedIn is one projected scan with a
        # ceiling. Past it "no badge" stops meaning "definitely new", and the
        # page has to be able to say so -- a silent cap reads as a clean file.
        from juicebox import duplicates

        monkeypatch.setattr(duplicates, "MAX_LINKEDIN_SCAN", 1)
        headers = account["headers"]
        _seed(
            client,
            headers,
            [
                {
                    "source": "manual",
                    "source_id": f"man-{i}",
                    "full_name": f"Person {i}",
                    "linkedin_url": f"https://linkedin.com/in/person-{i}",
                }
                for i in range(3)
            ],
        )
        body = _check(
            client, headers, [{"key": "0", "linkedin_url": "https://linkedin.com/in/person-2"}]
        )
        assert body["truncated"] is True

        # An address match is unaffected by that cap, and says so.
        monkeypatch.setattr(duplicates, "MAX_LINKEDIN_SCAN", 50_000)
        body = _check(
            client, headers, [{"key": "0", "linkedin_url": "https://linkedin.com/in/person-2"}]
        )
        assert body["truncated"] is False
        assert _by_key(body)["0"]["matched_on"] == "linkedin"
