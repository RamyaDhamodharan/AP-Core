import hashlib
import importlib
import logging
import os
import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    UploadFile,
)
from minio.error import S3Error
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....db.session import SessionLocal, get_db
from ....models.documents_models import Document
from ....services.llm_pipeline import (
    classify_document,
    extract_document,
    validate_extracted_data,
)
from ....storage.minio_storage import (
    upload_file,
    download_file,
    delete_file,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["Documents"])

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".eml"}
MAX_FILE_SIZE = 20 * 1024 * 1024
# Limit how many documents are processed at once (Docling + LLM are heavy).
_PROCESS_SLOTS = threading.BoundedSemaphore(
    int(os.getenv("PROCESS_CONCURRENCY", "2"))
)

# Docling loads big models when created, so build one converter and reuse it.
_converters: dict = {}


def _get_converter(converter_cls):
    if converter_cls not in _converters:
        _converters[converter_cls] = converter_cls()
    return _converters[converter_cls]


def _process_in_background(document_id: uuid.UUID) -> None:
    """Auto-run parse -> classify -> extract right after upload."""
    with _PROCESS_SLOTS:
        db = SessionLocal()
        try:
            document = db.query(Document).filter_by(id=document_id).first()
            if document is None or document.status != "received":
                return  # already handled
            parse_document(document_id=document_id, db=db)
        except HTTPException as exc:
            logger.error(
                "Auto processing failed for %s: %s", document_id, exc.detail
            )
        except Exception:
            logger.exception("Auto processing crashed for %s", document_id)
        finally:
            db.close()

@router.post("/upload", status_code=201)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    filename = Path(file.filename or "upload").name
    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type",
        )

    content = await file.read(MAX_FILE_SIZE + 1)

    if not content:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is empty",
        )

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="File exceeds 20 MB limit",
        )

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
        logger.exception("MinIO upload failed")
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
            logger.exception("MinIO cleanup failed after duplicate upload")

        raise HTTPException(
            status_code=409,
            detail="Duplicate file upload",
        ) from None

    except Exception:
        db.rollback()

        try:
            delete_file(storage_path)
        except Exception:
            logger.exception("MinIO cleanup failed after database error")

        raise
    background_tasks.add_task(_process_in_background, document.id)
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
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    if not document.storage_path:
        raise HTTPException(
            status_code=404,
            detail="Stored file not found",
        )

    try:
        content = download_file(document.storage_path)

    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            raise HTTPException(
                status_code=404,
                detail="Stored file not found",
            ) from None

        logger.exception("MinIO download failed")
        raise HTTPException(
            status_code=503,
            detail="File storage is unavailable",
        ) from None

    except Exception:
        logger.exception("MinIO download failed")
        raise HTTPException(
            status_code=503,
            detail="File storage is unavailable",
        ) from None

    temp_path = None

    try:
        # Step 1: Prepare the document for parsing
        with tempfile.NamedTemporaryFile(
            suffix=Path(document.original_filename).suffix.lower(),
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name

        document.status = "parsing"
        document.failure_message = None
        db.commit()

        # Step 2: Parse document using Docling
        import importlib

        DocumentConverter = importlib.import_module(
            "docling.document_converter"
        ).DocumentConverter
        converter = _get_converter(DocumentConverter)
        result = converter.convert(temp_path)

        raw_text = result.document.export_to_markdown()

        if not raw_text or not raw_text.strip():
            raise ValueError("No text could be extracted from the document")

        document.raw_text = raw_text
        document.status = "parsed"
        db.commit()

        # Step 3: Classify document using OpenRouter
        doc_type = classify_document(raw_text)

        # Supported database values: invoice, po, other
        if doc_type not in {"invoice", "po", "other"}:
            raise ValueError(
                f"Unsupported document type returned: {doc_type}"
            )

        document.doc_type = doc_type
        document.status = "classified"
        db.commit()

        # Step 4: Extract structured fields using Groq
        extracted_data = extract_document(raw_text, doc_type)

        # Step 5: Validate extracted fields
        validation_issues = validate_extracted_data(
            extracted_data,
            doc_type,
        )

        # Preserve the original extraction for manual review.
        document.extracted_json = extracted_data

        if validation_issues:
            document.status = "needs_vendor_review"
            document.failure_message = "; ".join(
                validation_issues
            )[:500]

            logger.warning(
                "Document %s requires review: %s",
                document_id,
                validation_issues,
            )
        else:
            document.status = "extracted"
            document.failure_message = None

        db.commit()
        db.refresh(document)

        return {
            "id": str(document.id),
            "status": document.status,
            "doc_type": document.doc_type,
            "extracted_json": document.extracted_json,
            "validation_issues": validation_issues,
        }

    except Exception as exc:
        logger.exception(
            "Document parse/classification/extraction failed for %s",
            document_id,
        )
        db.rollback()

        try:
            document = db.query(Document).filter_by(id=document_id).first()

            if document:
                document.status = "failed"
                document.failure_message = str(exc)[:500]
                db.commit()

        except Exception:
            db.rollback()
            logger.exception("Failed to update document failure status")

        raise HTTPException(
            status_code=500,
            detail=(
                "Document processing failed. "
                "Check Core service logs."
            ),
        ) from None

    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)
