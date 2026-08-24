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

    def test_a_number_cell_becomes_text_rather_than_1_0(self, client, account):
        payload = workbook([["Total Experience (Years)"], [9.5]])
        body = upload(client, account, payload).json()
        assert "9.5" in body["delimited"]

    def test_blank_rows_are_skipped_instead_of_becoming_empty_records(self, client, account):
        payload = workbook(
            [HEADER, [None] * len(HEADER), ["Bo", "Diddley"], ["", "", ""], ["Cy", "Young"]]
        )
        body = upload(client, account, payload).json()
        assert body["rows"] == 2

    def test_it_names_the_other_sheets_so_data_on_tab_two_is_not_invisible(
        self, client, account
    ):
        payload = workbook([HEADER, ["Ada", "Lovelace"]], sheets=["Notes", "Raw"])
        body = upload(client, account, payload).json()
        assert body["sheet"] == "Sheet1"
        assert body["sheets"] == ["Sheet1", "Notes", "Raw"]


class TestRefusals:
    def test_a_csv_is_refused_with_a_reason(self, client, account):
        response = upload(client, account, b"a,b\n1,2\n", name="export.csv")
        assert response.status_code == 400
        assert "browser" in response.json()["detail"]

    def test_a_file_that_is_not_a_workbook_is_a_400_not_a_500(self, client, account):
        response = upload(client, account, b"definitely not a zip")
        assert response.status_code == 400
        assert "Could not read" in response.json()["detail"]

    def test_an_empty_file_is_refused(self, client, account):
        response = upload(client, account, b"")
        assert response.status_code == 400

    def test_a_sheet_with_only_blank_rows_is_refused(self, client, account):
        response = upload(client, account, workbook([[None, None], ["", ""]]))
        assert response.status_code == 400
        assert "no rows" in response.json()["detail"]

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


class TestTruncation:
    def test_a_runaway_sheet_is_capped_and_says_so(self, client, account, monkeypatch):
        from juicebox.routers import imports

        monkeypatch.setattr(imports, "MAX_ROWS", 3)
        payload = workbook([HEADER] + [[f"Person{i}", "X"] for i in range(10)])
        body = upload(client, account, payload).json()
        assert body["truncated"] is True
        # 3 written including the header, so 2 data rows reported.
        assert body["rows"] == 2
