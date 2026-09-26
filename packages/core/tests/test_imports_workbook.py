"""The .xlsx round trip.

The route converts and returns; it persists nothing. So these tests are about
whether the text handed back is something the browser parser can consume --
particularly the quoting, because the whole reason a naive CSV split fails on a
Juicebox export is the commas inside Location and Education.
"""

from __future__ import annotations

import csv
import io

import pytest

openpyxl = pytest.importorskip("openpyxl")

HEADER = [
    "First name",
    "Last name",
    "Location",
    "LinkedIn",
    "Personal Email",
    "Personal Email Verification",
    "Current Title",
    "Current Org Name",
    "Education",
]


def workbook(rows, *, sheets: list[str] | None = None, title: str = "Sheet1") -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    for extra in sheets or []:
        book.create_sheet(extra)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def upload(client, account, payload: bytes, name: str = "export.xlsx"):
    return client.post(
        "/v1/imports/workbook",
        headers=account["headers"],
        files={
            "file": (
                name,
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )


class TestConversion:
    def test_a_workbook_comes_back_as_parseable_csv(self, client, account):
        payload = workbook(
            [
                HEADER,
                [
                    "Ada",
                    "Lovelace",
                    "San Francisco Bay Area, California, United States",
                    "https://linkedin.com/in/ada",
                    "ada@gmail.com",
                    "Deliverable",
                    "Research Engineer",
                    "Meta",
                    "Oxford, MA",
                ],
            ]
        )
        response = upload(client, account, payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["rows"] == 1
        assert body["sheet"] == "Sheet1"
        assert body["truncated"] is False

        # The commas inside Location and Education must survive as data, which
        # means they have to come back quoted.
        parsed = list(csv.DictReader(io.StringIO(body["delimited"])))
        assert len(parsed) == 1
        assert parsed[0]["Location"] == "San Francisco Bay Area, California, United States"
        assert parsed[0]["Education"] == "Oxford, MA"
        assert parsed[0]["Personal Email"] == "ada@gmail.com"


class TestRefusals:
    def test_an_oversized_upload_is_refused_rather_than_buffered(
        self, client, account, monkeypatch
    ):
        from juicebox.routers import imports

        monkeypatch.setattr(imports, "MAX_UPLOAD_BYTES", 64)
        response = upload(client, account, workbook([HEADER, ["Ada", "Lovelace"]]))
        assert response.status_code == 413

    def test_it_needs_authentication(self, client):
        response = client.post(
            "/v1/imports/workbook",
            files={"file": ("export.xlsx", b"x", "application/octet-stream")},
        )
        assert response.status_code in {401, 403}


