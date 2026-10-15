# Hot reload test
import hashlib
import logging
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from minio.error import S3Error
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.session import get_db
from src.models.documents_models import Document
from src.storage.minio_storage import (
    upload_file,
    download_file,
    delete_file,
)

router = APIRouter(prefix="/api/documents", tags=["Documents"])

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

    document_id = uuid.uuid4()
    storage_path = f"documents/{document_id}{extension}"

    try:
        upload_file(
            storage_path,
            content,
            file.content_type or "application/octet-stream",
        )
    except Exception:
        logging.exception("MinIO upload failed")
        raise HTTPException(
            status_code=503,
            detail="File storage is unavailable. Check MinIO logs.",
        ) from None

    document = Document(
        id=document_id,
        original_filename=filename,
        file_hash=file_hash,
        storage_path=storage_path,
        status="received",
    )

    try:
        db.add(document)
        db.commit()
        db.refresh(document)

    except IntegrityError:
        db.rollback()
        try:
            delete_file(storage_path)
        except Exception:
            logging.exception("MinIO cleanup failed after duplicate upload")

        raise HTTPException(
            status_code=409,
            detail="Duplicate file upload",
        ) from None

    except Exception:
        db.rollback()
        try:
            delete_file(storage_path)
        except Exception:
            logging.exception("MinIO cleanup failed after database error")
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
    document = db.query(Document).filter_by(id=document_id).first()

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    if not document.storage_path:
        raise HTTPException(status_code=404, detail="Stored file not found")

    try:
        content = download_file(document.storage_path)
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            raise HTTPException(
                status_code=404,
                detail="Stored file not found",
            ) from None

        logging.exception("MinIO download failed")
        raise HTTPException(
            status_code=503,
            detail="File storage is unavailable",
        ) from None
    except Exception:
        logging.exception("MinIO download failed")
        raise HTTPException(
            status_code=503,
            detail="File storage is unavailable",
        ) from None

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=Path(document.original_filename).suffix.lower(),
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name

        document.status = "parsing"
        db.commit()

        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        result = converter.convert(temp_path)
        raw_text = "\n".join(item.text for item in result.document.texts if getattr(item, "text", "").strip())

        if not raw_text or not raw_text.strip():
            raise ValueError("No text could be extracted from the document")

        document.raw_text = raw_text
        document.status = "parsed"
        document.failure_message = None

        db.commit()
        db.refresh(document)

        return {
            "id": str(document.id),
            "status": document.status,
            "raw_text": document.raw_text,
        }

    except Exception as exc:
        logging.exception("Document parsing failed for %s", document_id)
        db.rollback()

        try:
            document = db.query(Document).filter_by(id=document_id).first()

            if document:
                document.status = "failed"
                document.failure_message = str(exc)[:500]
                db.commit()
        except Exception:
            db.rollback()
            logging.exception("Failed to update document failure status")

        raise HTTPException(
            status_code=500,
            detail="Document parsing failed. Check Core service logs.",
        ) from None

    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)
