"""Exporting the whole CRM as CSV.

The grid pages at 100 and select-all stops at 1000, so before this route the
only export wrote the rows on screen. What matters here is that the file holds
exactly what the page header counts -- every row, the same filters -- and that
nothing in it runs as a spreadsheet formula when somebody opens it.
"""

from __future__ import annotations

import csv
import io
from datetime import timedelta

from juicebox.models import Message, utcnow
from juicebox.routers.candidates import EXPORT_COLUMNS, MAX_SELECT_ALL, _export_cell


def _bulk(client, headers, people):
    response = client.post("/v1/candidates/bulk", json={"candidates": people}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["ids"]


def _person(name, **extra):
    return {"full_name": name, "source": "test", "source_id": name.lower(), **extra}


def _export(client, headers, **params):
    response = client.get("/v1/candidates/export", params=params, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    text = response.text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    return response, rows[0], [dict(zip(rows[0], row, strict=True)) for row in rows[1:]]


class TestExport:
    def test_writes_every_row_past_the_select_all_cap(self, client, account):
        headers = account["headers"]
        total = MAX_SELECT_ALL + 25
        for start in range(0, total, 500):
            _bulk(
                client,
                headers,
                [_person(f"Person {n:05d}") for n in range(start, min(start + 500, total))],
            )

        response, header, rows = _export(client, headers)
        count = client.get("/v1/candidates/count", headers=headers).json()["count"]

        assert tuple(header) == EXPORT_COLUMNS
        assert len(rows) == count == total
        assert response.headers["x-row-count"] == str(total)
        # The grid's own default order, so the file reads the way the page does.
        assert [row["Name"] for row in rows[:2]] == ["Person 00000", "Person 00001"]

    def test_uses_the_same_filters_as_the_count(self, client, account):
        headers = account["headers"]
        _bulk(
            client,
            headers,
            [
                _person("Ada Lovelace", current_company="Analytical"),
                _person("Grace Hopper", current_company="Navy"),
            ],
        )

        _, _, rows = _export(client, headers, q="lovelace")

        assert [row["Name"] for row in rows] == ["Ada Lovelace"]
        assert client.get(
            "/v1/candidates/count", params={"q": "lovelace"}, headers=headers
        ).json()["count"] == 1

    def test_carries_contact_details_and_history(self, client, session, account):
        headers = account["headers"]
        [candidate_id] = _bulk(
            client,
            headers,
            [
                _person(
                    "Ada Lovelace",
                    current_title="Engineer",
                    current_company="Analytical",
                    emails=["ada@personal.dev", "ada@analytical.com"],
                    phones=["+14155550123"],
                    linkedin_url="https://www.linkedin.com/in/ada",
                )
            ],
        )
        for direction, days in (("outbound", 10), ("outbound", 5), ("inbound", 4)):
            session.add(
                Message(
                    org_id=account["org_id"],
                    enrollment_id=None,
                    candidate_id=candidate_id,
                    direction=direction,
                    source="microsoft",
                    status="sent" if direction == "outbound" else "received",
                    sent_at=utcnow() - timedelta(days=days),
                )
            )
        session.commit()

        _, _, [row] = _export(client, headers)

        assert row["Email"] == "ada@personal.dev"
        assert row["Other emails"] == "ada@analytical.com"
        # A leading "+" on a phone number is not a formula and must not be quoted.
        assert row["Phones"] == "+14155550123"
        assert row["LinkedIn"] == "https://www.linkedin.com/in/ada"
        assert row["Sent"] == "2"
        assert row["Replies"] == "1"
        assert row["Last reply"]
        assert row["Contact ID"] == candidate_id

    def test_an_empty_workspace_is_a_header_only_file(self, client, account):
        _, header, rows = _export(client, account["headers"])
        assert tuple(header) == EXPORT_COLUMNS
        assert rows == []

    def test_rejects_an_unknown_sort(self, client, account):
        response = client.get(
            "/v1/candidates/export", params={"sort": "salary"}, headers=account["headers"]
        )
        assert response.status_code == 422

    def test_needs_a_login(self, client):
        assert client.get("/v1/candidates/export").status_code == 401


class TestFormulaGuard:
    def test_formula_leads_are_neutralised(self):
        assert _export_cell("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
        assert _export_cell("@SUM(A1)") == "'@SUM(A1)"
        assert _export_cell("-2+3+cmd|' /C calc'!A0") == "'-2+3+cmd|' /C calc'!A0"

    def test_numbers_and_ordinary_text_are_left_alone(self):
        assert _export_cell("+1 (415) 555-0123") == "+1 (415) 555-0123"
        assert _export_cell("Ada Lovelace") == "Ada Lovelace"
        assert _export_cell(None) == ""
        assert _export_cell(3) == "3"
