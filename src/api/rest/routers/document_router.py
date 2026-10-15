
import hashlib
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.session import get_db
from src.models.documents_models import Document

router = APIRouter(prefix="/api/documents", tags=["Documents"])

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads"))
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".eml"}
MAX_FILE_SIZE = 20 * 1024 * 1024


@router.post("/upload", status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    filename = Path(file.filename or "upload").name
    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    content = await file.read(MAX_FILE_SIZE + 1)

    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 20 MB limit")

    file_hash = hashlib.sha256(content).hexdigest()
    existing = db.query(Document).filter_by(file_hash=file_hash).first()

    if existing:
        return {
            "id": str(existing.id),
            "status": "duplicate_blocked",
            "message": "This file has already been uploaded",
        }

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    document_id = uuid.uuid4()
    storage_path = UPLOAD_DIR / f"{document_id}{extension}"
    storage_path.write_bytes(content)

    document = Document(
        id=document_id,
        original_filename=filename,
        file_hash=file_hash,
        storage_path=str(storage_path),
        status="received",
    )

    try:
        db.add(document)
        db.commit()
        db.refresh(document)
    except IntegrityError:
        db.rollback()
        storage_path.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail="Duplicate file upload") from None
    except Exception:
        db.rollback()
        storage_path.unlink(missing_ok=True)
        raise

    return {
        "id": str(document.id),
        "original_filename": document.original_filename,
        "status": document.status,
    }

@router.post("/{document_id}/parse")
def parse_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    from docling.document_converter import DocumentConverter

    document = db.query(Document).filter_by(id=document_id).first()

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    if not document.storage_path or not Path(document.storage_path).is_file():
        raise HTTPException(status_code=404, detail="Stored file not found")

    try:
        document.status = "parsing"
        db.commit()

        converter = DocumentConverter()
        result = converter.convert(document.storage_path)
        raw_text = result.document.export_to_text()

        if not raw_text.strip():
            raise ValueError("No text could be extracted from the document")

        document.raw_text = raw_text
        document.status = "parsed"
        db.commit()
        db.refresh(document)

        return {
            "id": str(document.id),
            "status": document.status,
            "raw_text": document.raw_text,
        }

    except Exception as exc:
        db.rollback()
        document = db.query(Document).filter_by(id=document_id).first()
        if document:
            document.status = "failed"
            document.failure_message = str(exc)[:500]
            db.commit()

        raise HTTPException(
            status_code=500,
            detail="Document parsing failed. Check Core service logs.",
        ) from None
