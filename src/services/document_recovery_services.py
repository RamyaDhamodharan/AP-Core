"""Recover documents whose background task stopped unexpectedly."""

import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_models import EmailMessage

logger = logging.getLogger(__name__)


def _document_attempts(db, document_id) -> int:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "document",
            AuditLog.entity_id == document_id,
            AuditLog.action == "document_stuck_requeued",
        )
        .count()
    )


def _recover_raw_email(db, document: Document) -> str:
    """Extract supported attachments from a persisted raw EML root, idempotently."""
    from src.services.document_ingest_services import ingest_file
    from src.services.email_polling_services import (
        _attachment_format,
        _referenced_content_ids,
        _safe_filename,
        _skip_image_part,
    )
    from src.storage.minio_storage import download_file

    if not document.storage_path:
        raise RuntimeError("Raw email is missing its stored message")
    raw = download_file(document.storage_path)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    referenced_cids = _referenced_content_ids(message)
    children = (
        db.query(Document).filter(Document.parent_document_id == document.id).all()
    )
    existing_hashes = {child.file_hash for child in children}
    created_or_found = 0
    attachment_candidate_seen = False
    inline_image_seen = False
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if not (filename or disposition in {"attachment", "inline"}):
            continue
        attachment_candidate_seen = True
        payload = part.get_payload(decode=True) or b""
        content_type = part.get_content_type().casefold()
        if _skip_image_part(part, payload, referenced_cids):
            continue
        detected = _attachment_format(part, payload)
        if detected is None:
            inline_image_seen = inline_image_seen or (
                content_type.startswith("image/")
                and disposition == "inline"
                and len(payload) >= 20 * 1024
            )
            continue
        extension, _ = detected
        safe_name = _safe_filename(filename or f"attachment{extension}", extension)
        digest = hashlib.sha256(payload).hexdigest()
        existing = db.query(Document).filter_by(file_hash=digest).first()
        if existing or digest in existing_hashes:
            created_or_found += 1
            continue
        result = ingest_file(
            db,
            safe_name,
            payload,
            content_type,
            None,
            "email",
            parent_document_id=document.id,
            email_meta={
                "email_sender": document.email_sender,
                "email_subject": document.email_subject,
                "email_received_at": document.email_received_at,
                "email_message_id": document.email_message_id,
                "email_uid": document.email_uid,
                "email_body_preview": document.email_body_preview,
                "email_eml_key": document.email_eml_key or document.storage_path,
            },
            document_id=uuid.uuid4(),
        )
        if result.status in {"received", "duplicate_blocked"}:
            created_or_found += 1
        else:
            raise RuntimeError(result.message or "Attachment could not be saved")

    if not created_or_found:
        if inline_image_seen:
            return "Only an inline image found, send a PDF"
        if attachment_candidate_seen:
            return "Unsupported attachment type"
        return "No attachment found"
    return "attachments recovered"


def sweep_stuck_documents(db, *, now: datetime | None = None) -> int:
    current = now or datetime.now(UTC)
    timeout_minutes = max(1, int(os.getenv("DOCUMENT_STUCK_TIMEOUT_MINUTES", "15")))
    max_attempts = max(1, int(os.getenv("DOCUMENT_PROCESS_MAX_ATTEMPTS", "3")))
    cutoff = current - timedelta(minutes=timeout_minutes)
    stuck = (
        db.query(Document)
        .filter(
            Document.status.in_(["received", "parsing", "parsed", "classified"]),
            Document.updated_at < cutoff,
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    outcomes = {
        "scanned": len(stuck),
        "requeued": 0,
        "recovered": 0,
        "failed": 0,
        "needs_attention": 0,
    }
    for document in stuck:
        if (
            document.source == "email"
            and document.parent_document_id is None
            and (
                document.original_filename.casefold().endswith(".eml")
                or (document.mime or "").casefold() == "message/rfc822"
            )
        ):
            try:
                outcome = _recover_raw_email(db, document)
            except Exception as exc:
                from src.services.email_polling_services import (
                    _sanitize_exception_message,
                )

                detail = _sanitize_exception_message(str(exc))
                outcome = (
                    f"Raw email recovery failed: {detail}"
                    if detail
                    else f"Raw email recovery failed: {type(exc).__name__}"
                )[:200]
            if outcome in {
                "No attachment found",
                "Only an inline image found, send a PDF",
                "Unsupported attachment type",
            }:
                document.status = "ignored"
                document.failure_message = outcome
                outcomes["needs_attention"] += 1
                message = (
                    db.query(EmailMessage)
                    .filter(EmailMessage.message_id == document.email_message_id)
                    .first()
                )
                if message:
                    message.status = "needs_attention"
                    message.failure_reason = outcome
                    message.filter_reason = outcome
                    message.next_retry_at = None
            elif outcome.startswith("Raw email recovery failed:"):
                document.status = "failed"
                document.failure_message = outcome
                outcomes["failed"] += 1
                message = (
                    db.query(EmailMessage)
                    .filter(EmailMessage.message_id == document.email_message_id)
                    .first()
                )
                if message:
                    message.status = "failed_final"
                    message.failure_reason = outcome
                    message.next_retry_at = None
            else:
                document.status = "processed"
                document.failure_message = None
                outcomes["recovered"] += 1
                message = (
                    db.query(EmailMessage)
                    .filter(EmailMessage.message_id == document.email_message_id)
                    .first()
                )
                if message:
                    message.status = "processed"
                    message.failure_reason = None
                    message.filter_reason = None
            db.add(
                AuditLog(
                    action="document_raw_email_recovered",
                    entity_type="document",
                    entity_id=document.id,
                    result="success"
                    if not outcome.startswith("Raw email")
                    else "error",
                    message="Stuck raw email recovery completed",
                    reason=outcome,
                    new_value={"outcome": outcome},
                )
            )
            logger.info(
                "stuck_document_recovery %s",
                json.dumps({"document_id": str(document.id), "outcome": outcome}),
            )
            continue

        attempts = _document_attempts(db, document.id)
        if attempts >= max_attempts:
            reason = (
                f"Processing did not start after {attempts} recovery attempts. "
                "Retry processing."
            )
            document.status = "failed"
            document.failure_message = reason
            outcomes["failed"] += 1
            outcome = "failed_max_attempts"
        else:
            document.status = "parsing"
            document.failure_message = None
            outcomes["requeued"] += 1
            outcome = "requeued"
        db.add(
            AuditLog(
                action="document_stuck_requeued"
                if outcome == "requeued"
                else "document_stuck_failed",
                entity_type="document",
                entity_id=document.id,
                result="success" if outcome == "requeued" else "error",
                message="Stuck document recovery processed",
                reason=document.failure_message,
                new_value={"outcome": outcome, "attempt": attempts + 1},
            )
        )
        db.commit()
        if outcome == "requeued":
            try:
                from src.worker.tasks import process_document

                process_document.apply_async(args=[str(document.id)], queue="documents")
            except Exception as exc:
                reason = f"Could not queue document recovery: {type(exc).__name__}"
                document.status = "failed"
                document.failure_message = reason
                outcomes["requeued"] -= 1
                outcomes["failed"] += 1
                outcome = "queue_failed"
                db.add(
                    AuditLog(
                        action="document_stuck_failed",
                        entity_type="document",
                        entity_id=document.id,
                        result="error",
                        message="Stuck document recovery could not be queued",
                        reason=reason,
                        new_value={"outcome": outcome, "attempt": attempts + 1},
                    )
                )
                db.commit()
        logger.info(
            "stuck_document_recovery %s",
            json.dumps(
                {
                    "document_id": str(document.id),
                    "outcome": outcome,
                    "attempt": attempts + 1,
                    "reason": document.failure_message,
                },
                sort_keys=True,
            ),
        )

    failed_email_documents = (
        db.query(Document)
        .filter(Document.source == "email", Document.status == "failed")
        .all()
    )
    for document in failed_email_documents:
        if (
            document.source != "email"
            or document.status != "failed"
            or not document.email_message_id
        ):
            continue
        message = (
            db.query(EmailMessage)
            .filter(EmailMessage.message_id == document.email_message_id)
            .first()
        )
        if message and message.status not in {
            "failed",
            "failed_final",
            "needs_attention",
        }:
            message.status = "failed_final"
            message.failure_reason = (
                document.failure_message or "Email attachment processing failed"
            )[:200]
            message.next_retry_at = None
    db.commit()
    logger.info("stuck_document_recovery_run %s", json.dumps(outcomes, sort_keys=True))
    return len(stuck)
