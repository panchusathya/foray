"""Commit-search parsing. Payloads follow the GitHub Search Commits API shape
(``GET /search/commits``), including the live 2026-08-20 finding that
``author:danijar`` returns ``mail@danijar.org`` with ``author.login = danijar``.
"""

from __future__ import annotations

from foray.enrich.commit_search import extract_attributed_commit_emails
from foray.enrich.contacts import is_placeholder
from foray.enrich.emails import is_usable_commit_email

# Captured-shape Search API items: GitHub-attributed author.login, git
# author.email, author-date. The mismatched login is the rejection case.
DANIJAR_SEARCH_ITEMS = [
    {
        "sha": "a" * 40,
        "commit": {
            "author": {
                "name": "Danijar Hafner",
                "email": "mail@danijar.org",
                "date": "2024-06-11T18:04:22Z",
            }
        },
        "author": {"login": "danijar", "id": 1139593},
    },
    {
        "sha": "b" * 40,
        "commit": {
            "author": {
                "name": "Danijar Hafner",
                "email": "mail@danijar.com",
                "date": "2021-03-01T00:00:00Z",
            }
        },
        "author": {"login": "danijar", "id": 1139593},
    },
    {
        "sha": "c" * 40,
        "commit": {
            "author": {
                "name": "Someone Else",
                "email": "intruder@gmail.com",
                "date": "2024-01-01T00:00:00Z",
            }
        },
        "author": {"login": "not-danijar", "id": 1},
    },
    {
        "sha": "d" * 40,
        "commit": {
            "author": {
                "name": "Unlinked",
                "email": "ghost@gmail.com",
                "date": "2024-01-02T00:00:00Z",
            }
        },
        "author": None,
    },
]


def test_attributed_emails_counted_and_dated():
    found = extract_attributed_commit_emails(DANIJAR_SEARCH_ITEMS, "danijar")
    assert found["mail@danijar.org"]["occurrences"] == 1
    assert found["mail@danijar.org"]["last_date"] == "2024-06-11T18:04:22Z"
    assert found["mail@danijar.com"]["occurrences"] == 1


def test_login_mismatch_is_rejected():
    found = extract_attributed_commit_emails(DANIJAR_SEARCH_ITEMS, "danijar")
    assert "intruder@gmail.com" not in found
    assert "ghost@gmail.com" not in found


def test_login_match_is_case_insensitive():
    found = extract_attributed_commit_emails(DANIJAR_SEARCH_ITEMS, "Danijar")
    assert "mail@danijar.org" in found


def test_noreply_still_dropped():
    items = [
        {
            "commit": {"author": {"email": "123+danijar@users.noreply.github.com", "date": "2024-01-01T00:00:00Z"}},
            "author": {"login": "danijar"},
        }
    ]
    assert extract_attributed_commit_emails(items, "danijar") == {}


def test_incident_addresses_survive_every_new_gate():
    """mail@danijar.com / email@jamie.sh / mail@taeuk.me must still be writable."""
    for email in ("mail@danijar.com", "email@jamie.sh", "mail@taeuk.me"):
        assert is_usable_commit_email(email) is True
        assert is_placeholder(email) is False
