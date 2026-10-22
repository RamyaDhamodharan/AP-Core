"""Shared file ingestion for browser uploads and email attachments."""

import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import BackgroundTasks
from sqlalchemy.exc import IntegrityError

from src.models.audit_models import AuditLog
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
    object_key: str | None = None,
    document_id: uuid.UUID | None = None,
) -> IngestResult:
    """Persist one file and enqueue document processing after its row commits."""
    ingest_started = time.perf_counter()
    filename = Path(filename or "upload").name
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return IngestResult("failed", None, "Unsupported file type")
    if not content_bytes:
        return IngestResult("failed", None, "Uploaded file is empty")
    max_size = (
        int(os.getenv("EMAIL_MAX_ATTACHMENT_MB", "20")) * 1024 * 1024
        if source == "email_poll" and extension != ".eml"
        else MAX_FILE_SIZE
    )
    if len(content_bytes) > max_size:
        if max_size % (1024 * 1024) == 0:
            limit_label = f"{max_size // (1024 * 1024)} MB"
        else:
            limit_label = f"{max_size} byte"
        return IngestResult("failed", None, f"File exceeds {limit_label} limit")

    file_hash = hashlib.sha256(content_bytes).hexdigest()
    existing = db.query(Document).filter_by(file_hash=file_hash).first()
    if existing:
        return IngestResult(
            "duplicate_blocked", existing.id, "This file has already been uploaded"
        )

    document_id = document_id or uuid.uuid4()
    received_at = datetime.now(UTC)
    storage_path = object_key or (
        f"incoming/{received_at:%Y}/{received_at:%m}/{document_id}/{filename}"
    )
    try:
        upload_file(
            storage_path, content_bytes, content_type or "application/octet-stream"
        )
        document = Document(
            id=document_id,
            original_filename=filename,
            file_hash=file_hash,
            sha256=file_hash,
            mime=content_type,
            size=len(content_bytes),
            storage_path=storage_path,
            status="received",
            created_by=user_id,
            updated_by=user_id,
            parent_document_id=parent_document_id,
            source=source,
            **(email_meta or {}),
        )
        db.add(document)
        db.add(
            AuditLog(
                action="pipeline_step",
                entity_type="document",
                entity_id=document.id,
                step_name="receive",
                duration_ms=max(0, int((time.perf_counter() - ingest_started) * 1000)),
                input_ref=storage_path,
                output_summary={"filename": filename, "size_bytes": len(content_bytes)},
                result="success",
                message="Document received",
                created_by=user_id,
            )
        )
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
            if source == "email_poll":
                logger.warning("Email MinIO cleanup failed (%s)", type(exc).__name__)
            else:
                logger.exception("MinIO cleanup failed after ingestion error")
        if source == "email_poll":
            logger.warning("Email file ingestion failed (%s)", type(exc).__name__)
            raise RuntimeError(
                "Email file persistence failed while saving to storage/database: "
                f"{type(exc).__name__}: {exc}"
            ) from None
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
