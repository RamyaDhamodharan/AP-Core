"""Periodic recovery for documents whose background task stopped unexpectedly."""

import os
from datetime import UTC, datetime, timedelta

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_models import EmailMessage


def sweep_stuck_documents(db, *, now: datetime | None = None) -> int:
    current = now or datetime.now(UTC)
    timeout_minutes = max(1, int(os.getenv("DOCUMENT_STUCK_TIMEOUT_MINUTES", "60")))
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
    changed = 0
    for document in stuck:
        reason = (
            f"Processing stopped unexpectedly after {timeout_minutes} minutes. "
            "Retry processing."
        )
        old_status = document.status
        document.status = "failed"
        document.failure_message = reason
        db.add(
            AuditLog(
                action="document_processing_stuck",
                entity_type="document",
                entity_id=document.id,
                result="error",
                old_value={"status": old_status},
                new_value={"status": "failed", "reason": reason},
                message="A stalled document was marked failed for retry",
                reason=reason,
            )
        )
        changed += 1

    failed_email_documents = (
        db.query(Document)
        .filter(Document.source == "email", Document.status == "failed")
        .all()
    )
    for document in failed_email_documents:
        if not document.email_message_id:
            continue
        message = (
            db.query(EmailMessage)
            .filter(EmailMessage.message_id == document.email_message_id)
            .first()
        )
        if message and message.status not in {"failed", "failed_final"}:
            message.status = "failed_final"
            message.failure_reason = (
                document.failure_message or "Email attachment processing failed"
            )[:200]
            message.next_retry_at = None
            changed += 1
    if changed:
        db.commit()
    return changed
