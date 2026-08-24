"""Candidate document upload and download — the repo's first file endpoints.

Three rules the tests pin:

* **The cap is enforced while reading**, in chunks — a 200 MB body is refused
  at the boundary, never buffered and weighed afterwards.
* **Type is verified by magic bytes**, not the client's Content-Type header. A
  PDF starts with ``%PDF-``; a header is a claim.
* **Downloads never build paths from client input.** The row's
  ``storage_path`` was server-built from ids; the route only ever looks up an
  org-scoped row and streams what it names.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..config import get_settings
from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..jobs import KIND_DOC_PARSE, enqueue
from ..models import Candidate, CandidateDocument
from ..schemas import CandidateDocumentOut

logger = logging.getLogger(__name__)

router = APIRouter(
    route_class=CommittingRoute,
    prefix="/v1/candidates/{candidate_id}/documents",
    tags=["documents"],
)

_CHUNK = 1024 * 1024
_PDF_MAGIC = b"%PDF-"

UPLOAD_KINDS = {"resume_uploaded", "other"}


def _load_candidate(db: Session, candidate_id: str, org_id: str) -> Candidate:
    candidate = db.scalar(
        select(Candidate).where(Candidate.id == candidate_id, Candidate.org_id == org_id)
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return candidate


def _load_document(
    db: Session, *, document_id: str, candidate_id: str, org_id: str
) -> CandidateDocument:
    doc = db.scalar(
        select(CandidateDocument).where(
            CandidateDocument.id == document_id,
            CandidateDocument.candidate_id == candidate_id,
            CandidateDocument.org_id == org_id,
        )
    )
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return doc


async def _read_capped(upload: UploadFile) -> bytes:
    cap = get_settings().max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds the {cap // (1024 * 1024)} MB limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("", response_model=CandidateDocumentOut, status_code=201)
async def upload_document(
    candidate_id: str,
    file: UploadFile = File(...),
    kind: str = Query(default="resume_uploaded"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> CandidateDocumentOut:
    if kind not in UPLOAD_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"kind must be one of {sorted(UPLOAD_KINDS)}",
        )
    candidate = _load_candidate(db, candidate_id, principal.org_id)
    data = await _read_capped(file)
    if not data.startswith(_PDF_MAGIC):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF files are accepted (content did not read as a PDF)",
        )

    doc = CandidateDocument(
        org_id=principal.org_id,
        candidate_id=candidate.id,
        kind=kind,
        filename=(file.filename or "upload.pdf")[:255],
        content_type="application/pdf",
        status="uploaded",
        source="upload",
        created_by=principal.user_id,
    )
    if kind == "resume_uploaded":
        # The newest upload is the profile resume; the previous default steps
        # down in the same transaction so there is never zero or two.
        db.execute(
            CandidateDocument.__table__.update()
            .where(
                CandidateDocument.candidate_id == candidate.id,
                CandidateDocument.kind == "resume_uploaded",
                CandidateDocument.is_default.is_(True),
            )
            .values(is_default=False)
        )
        doc.is_default = True
    db.add(doc)
    db.flush()

    relpath = storage.document_relpath(
        org_id=doc.org_id, candidate_id=doc.candidate_id, document_id=doc.id
    )
    size, digest = storage.save_bytes(relpath, data)
    doc.storage_path = relpath
    doc.size_bytes = size
    doc.sha256 = digest
    db.flush()

    if kind == "resume_uploaded":
        enqueue(
            db,
            org_id=principal.org_id,
            kind=KIND_DOC_PARSE,
            idempotency_key=f"{KIND_DOC_PARSE}:{doc.id}",
            payload={"document_id": doc.id},
        )
    return CandidateDocumentOut.model_validate(doc)


@router.get("", response_model=list[CandidateDocumentOut])
def list_documents(
    candidate_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> list[CandidateDocumentOut]:
    _load_candidate(db, candidate_id, principal.org_id)
    rows = db.scalars(
        select(CandidateDocument)
        .where(
            CandidateDocument.candidate_id == candidate_id,
            CandidateDocument.org_id == principal.org_id,
        )
        .order_by(CandidateDocument.created_at.desc())
    ).all()
    return [CandidateDocumentOut.model_validate(row) for row in rows]


@router.get("/{document_id}/download")
def download_document(
    candidate_id: str,
    document_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> Response:
    doc = _load_document(
        db, document_id=document_id, candidate_id=candidate_id, org_id=principal.org_id
    )
    if not doc.storage_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No file stored")
    try:
        data = storage.read_bytes(doc.storage_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File missing from store"
        ) from exc
    filename = (doc.filename or f"{doc.id}.pdf").replace('"', "")
    return Response(
        content=data,
        media_type=doc.content_type or "application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/{document_id}", status_code=204)
def delete_document(
    candidate_id: str,
    document_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> Response:
    doc = _load_document(
        db, document_id=document_id, candidate_id=candidate_id, org_id=principal.org_id
    )
    if doc.storage_path:
        storage.delete_file(doc.storage_path)
    db.delete(doc)
    db.flush()
    return Response(status_code=204)
