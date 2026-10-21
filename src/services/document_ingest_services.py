"""Shared file ingestion for browser uploads and email attachments."""

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import BackgroundTasks
from sqlalchemy.exc import IntegrityError

from src.models.document_models import Document
from src.storage.minio_storage import delete_file, upload_file
from src.worker.dispatch import dispatch_document_processing

logger = logging.getLogger(__name__)
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".eml"}
MAX_FILE_SIZE = 20 * 1024 * 1024


@dataclass
class IngestResult:
    status: str
    document_id: uuid.UUID | None
    message: str


def ingest_file(
    db,
    filename: str,
    content_bytes: bytes,
    content_type: str | None,
    user_id: uuid.UUID | None,
    source: str,
    parent_document_id: uuid.UUID | None = None,
    email_meta: dict | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> IngestResult:
    """Persist one file and enqueue document processing after its row commits."""
    filename = Path(filename or "upload").name
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return IngestResult("failed", None, "Unsupported file type")
    if not content_bytes:
        return IngestResult("failed", None, "Uploaded file is empty")
    if len(content_bytes) > MAX_FILE_SIZE:
        return IngestResult("failed", None, "File exceeds 20 MB limit")

    file_hash = hashlib.sha256(content_bytes).hexdigest()
    existing = db.query(Document).filter_by(file_hash=file_hash).first()
    if existing:
        return IngestResult(
            "duplicate_blocked", existing.id, "This file has already been uploaded"
        )

    document_id = uuid.uuid4()
    storage_path = f"documents/{document_id}{extension}"
    try:
        upload_file(
            storage_path, content_bytes, content_type or "application/octet-stream"
        )
        document = Document(
            id=document_id,
            original_filename=filename,
            file_hash=file_hash,
            storage_path=storage_path,
            status="received",
            created_by=user_id,
            updated_by=user_id,
            parent_document_id=parent_document_id,
            source=source,
            **(email_meta or {}),
        )
        db.add(document)
        db.commit()
        db.refresh(document)
    except IntegrityError:
        db.rollback()
        try:
            delete_file(storage_path)
        except Exception:
            logger.exception("MinIO cleanup failed after duplicate ingestion")
        existing = db.query(Document).filter_by(file_hash=file_hash).first()
        return IngestResult(
            "duplicate_blocked",
            existing.id if existing else None,
            "This file has already been uploaded",
        )
    except Exception as exc:
        db.rollback()
        try:
            delete_file(storage_path)
        except Exception:
            logger.exception("MinIO cleanup failed after ingestion error")
        logger.exception("File ingestion failed for %s", filename)
        return IngestResult("failed", None, str(exc)[:500] or "File ingestion failed")

    if extension != ".eml":
        from src.api.rest.routers.document_routers import _process_in_background

        if background_tasks is not None:
            dispatch_document_processing(
                background_tasks, document.id, _process_in_background
            )
        elif os.getenv("USE_CELERY", "false").lower() == "true":
            dispatch_document_processing(
                BackgroundTasks(), document.id, _process_in_background
            )
        else:
            _process_in_background(document.id)
    return IngestResult("received", document.id, "File received")
