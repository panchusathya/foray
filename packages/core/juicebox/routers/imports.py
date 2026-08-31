"""What the browser-side CSV importer cannot work out for itself. Nothing is persisted.

Two routes, both read-only: ``/workbook`` converts an ``.xlsx`` to delimited
text, and ``/known`` says which rows of a file are already contacts.

The CSV importer parses in the browser on purpose (see
``packages/web/src/lib/csvImport.js``): the recruiter reviews every row before
anything is written. An ``.xlsx`` workbook is a zip of XML that a browser cannot
read without a parsing library, so the file makes one round trip here and comes
back as CSV -- and the client-side mapper, the review table and its tests all
carry on unchanged.

Neither route writes anything, so this router has no ``CommittingRoute``, no
idempotency key and no audit row: asking either question twice has no effect
beyond returning the same answer. The only thing worth guarding is size,
because a whole sheet is held in memory.
"""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from ..deps import Principal, get_db, get_principal
from ..duplicates import Identity, known_contacts
from ..schemas import (
    KnownContactMatch,
    KnownContactsRequest,
    KnownContactsResponse,
)

router = APIRouter(prefix="/v1/imports", tags=["imports"])

# A Juicebox export is a few hundred rows; 10MB is far beyond any real one and
# still small enough that holding it in memory is not a denial of service.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Enough to stop a runaway sheet, generous enough that no real export hits it.
# /v1/candidates/bulk caps a request at 5000 and the client chunks to it, so
# this is about the conversion rather than about the import.
MAX_ROWS = 20_000

_EXTENSIONS = (".xlsx", ".xlsm")

# The duplicate check runs over the *whole* file at once -- the review table
# shows every row -- so this is MAX_ROWS-shaped rather than the 500 the client
# chunks /v1/candidates/bulk into. Matched to /bulk's own ceiling.
MAX_KNOWN = 5000


@router.post("/workbook")
async def workbook_to_csv(
    file: UploadFile = File(...),
    principal: Principal = Depends(get_principal),
) -> dict:
    """Read the first worksheet and return it as CSV text.

    ``data_only=True`` so a formula yields its last computed value rather than
    the formula string -- a workbook someone built by pasting and re-deriving
    columns is the common case, and ``=A2&" "&B2`` is not a name.
    """
    name = (file.filename or "").lower()
    if not name.endswith(_EXTENSIONS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload an .xlsx workbook. A .csv file is read in the browser directly.",
        )

    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"That workbook is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)}MB.",
        )
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="That file is empty."
        )

    try:
        import openpyxl
    except ModuleNotFoundError:  # pragma: no cover - dependency is declared
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Spreadsheet support is not installed on this server.",
        ) from None

    try:
        book = openpyxl.load_workbook(
            io.BytesIO(payload), read_only=True, data_only=True
        )
    except Exception as exc:
        # openpyxl raises a zoo of exception types for a corrupt or non-xlsx
        # file. The user only needs to know it could not be read.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not read that workbook: {type(exc).__name__}",
        ) from exc

    try:
        sheet = book[book.sheetnames[0]] if book.sheetnames else None
        if sheet is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That workbook has no sheets.",
            )

        buffer = io.StringIO()
        # QUOTE_MINIMAL and the standard dialect, so the text this returns is
        # exactly what the browser parser already handles -- including the
        # commas inside Location and Education that make a naive split fail.
        writer = csv.writer(buffer, lineterminator="\n")
        rows_written = 0
        truncated = False
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if value is None else str(value) for value in row]
            # Skip fully blank rows rather than emitting a row of empty fields;
            # a trailing block of them is normal in a hand-edited workbook.
            if not any(cell.strip() for cell in cells):
                continue
            if rows_written >= MAX_ROWS:
                truncated = True
                break
            writer.writerow(cells)
            rows_written += 1
    finally:
        book.close()

    if not rows_written:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That sheet has no rows.",
        )

    return {
        "delimited": buffer.getvalue(),
        # Reported so the page can say which sheet it read: a workbook whose
        # data is on the second tab would otherwise look silently empty.
        "sheet": sheet.title,
        "sheets": list(book.sheetnames),
        # rows_written counts the header, which the client's parser consumes.
        "rows": max(rows_written - 1, 0),
        "truncated": truncated,
    }


@router.post("/known", response_model=KnownContactsResponse)
def known_contacts_check(
    payload: KnownContactsRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> KnownContactsResponse:
    """Which rows of an import are already contacts. Writes nothing.

    Same reason ``/workbook`` lives here: the CSV importer parses in the
    browser so the recruiter reviews every row before anything is written, and
    the browser has no view of the CRM. This answers the one question it
    cannot answer for itself. Read-only, so no ``CommittingRoute``, no
    idempotency key and no audit row -- asking twice has no effect.

    It is deliberately advice, not enforcement. ``upsert_candidates`` still
    merges on ``(source, source_id)`` and a duplicate the recruiter chooses to
    keep still imports; this exists so that choice is an informed one.
    """
    if len(payload.people) > MAX_KNOWN:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Check at most {MAX_KNOWN} rows per request",
        )
    result = known_contacts(
        db,
        org_id=principal.org_id,
        identities=[
            Identity(
                key=person.key,
                emails=tuple(person.emails),
                linkedin_url=person.linkedin_url,
                full_name=person.full_name,
                company=person.company,
            )
            for person in payload.people
        ],
    )
    return KnownContactsResponse(
        matches=[
            KnownContactMatch(
                key=match.key,
                candidate_id=match.candidate_id,
                full_name=match.full_name,
                matched_on=match.matched_on,
                reason=match.reason,
            )
            for match in result.matches
        ],
        truncated=result.truncated,
    )
