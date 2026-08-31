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
    def test_a_known_address_is_matched(self, client, loaded_org):
        headers = loaded_org["headers"]
        body = _check(
            client,
            headers,
            [
                {"key": "0", "emails": ["ada@example.com"], "full_name": "Ada Okonkwo"},
                {"key": "1", "emails": ["nobody@example.com"], "full_name": "Nobody Here"},
            ],
        )
        matches = _by_key(body)
        assert set(matches) == {"0"}
        assert matches["0"]["matched_on"] == "email"
        assert matches["0"]["full_name"] == "Ada Okonkwo"
        assert "ada@example.com" in matches["0"]["reason"]
        assert matches["0"]["candidate_id"]

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
    def test_the_same_profile_under_a_different_source_is_matched(self, client, account):
        # The whole point. This person is on file from Foray sourcing; the CSV
        # row for them carries source "juicebox_csv", so (source, source_id)
        # sees two different people and upsert_candidates creates a second row.
        headers = account["headers"]
        _seed(
            client,
            headers,
            [
                {
                    "source": "foray",
                    "source_id": "gh:adalovelace",
                    "full_name": "Ada Lovelace",
                    "linkedin_url": "https://linkedin.com/in/ada-lovelace",
                    "emails": ["ada.l@analytical.engine"],
                }
            ],
        )
        body = _check(
            client,
            headers,
            [
                {
                    "key": "0",
                    # A different spelling, a different address, same person.
                    "linkedin_url": "http://www.linkedin.com/in/Ada-Lovelace/",
                    "emails": ["ada@personal.example.com"],
                    "full_name": "Ada Lovelace",
                }
            ],
        )
        match = _by_key(body)["0"]
        assert match["matched_on"] == "linkedin"
        assert match["full_name"] == "Ada Lovelace"

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

    def test_a_profile_nobody_has_is_not_matched(self, client, loaded_org):
        headers = loaded_org["headers"]
        body = _check(
            client,
            headers,
            [{"key": "0", "linkedin_url": "https://linkedin.com/in/nobody-at-all"}],
        )
        assert body["matches"] == []

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
    def test_email_wins_over_a_weaker_match(self, client, account):
        headers = account["headers"]
        _seed(
            client,
            headers,
            [
                {
                    "source": "manual",
                    "source_id": "man-1",
                    "full_name": "Ada Lovelace",
                    "emails": ["ada@example.com"],
                    "linkedin_url": "https://linkedin.com/in/ada-lovelace",
                    "experiences": [{"company": "Stripe", "title": "Eng", "is_current": True}],
                }
            ],
        )
        body = _check(
            client,
            headers,
            [
                {
                    "key": "0",
                    "emails": ["ada@example.com"],
                    "linkedin_url": "https://linkedin.com/in/ada-lovelace",
                    "full_name": "Ada Lovelace",
                    "company": "Stripe",
                }
            ],
        )
        assert _by_key(body)["0"]["matched_on"] == "email"

    def test_one_match_per_row_even_with_several_signals(self, client, loaded_org):
        headers = loaded_org["headers"]
        body = _check(
            client,
            headers,
            [{"key": "0", "emails": ["ada@example.com", "bo@example.com"]}],
        )
        assert len(body["matches"]) == 1

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

    def test_an_empty_request_is_an_empty_answer(self, client, loaded_org):
        body = _check(client, loaded_org["headers"], [])
        assert body == {"matches": [], "truncated": False}

    def test_a_row_with_nothing_to_match_on_is_skipped(self, client, loaded_org):
        body = _check(client, loaded_org["headers"], [{"key": "0"}])
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

    def test_too_many_rows_is_refused(self, client, loaded_org):
        from juicebox.routers.imports import MAX_KNOWN

        people = [{"key": str(i), "emails": [f"p{i}@example.com"]} for i in range(MAX_KNOWN + 1)]
        response = client.post(
            "/v1/imports/known", json={"people": people}, headers=loaded_org["headers"]
        )
        assert response.status_code == 413
