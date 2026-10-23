"""Admin endpoints for on-demand email polling and poll status."""

import os
from collections import Counter
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.api.rest.dependencies import require_role
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_models import EmailMessage, EmailPollState
from src.services.email_polling_services import poll_email_with_lock
from src.worker.dispatch import dispatch_document_processing

router = APIRouter(prefix="/api/email-polling", tags=["email-polling"])


@router.post("/run")
def run_email_poll(
    db: Session = Depends(get_db),
    _user: dict = Depends(require_role("admin")),
):
    return poll_email_with_lock(db)


@router.get("/status")
def email_poll_status(
    db: Session = Depends(get_db),
    _user: dict = Depends(require_role("admin")),
):
    mailbox = os.getenv("IMAP_USER", "").strip()
    folder = os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX"
    state = db.query(EmailPollState).filter_by(mailbox=mailbox, folder=folder).first()
    interval = max(1, int(os.getenv("EMAIL_POLL_INTERVAL_SECONDS", "300")))
    last_run = state.last_run_at if state else None
    messages = db.query(EmailMessage).all()
    failures = sum(row.status in {"failed", "failed_final"} for row in messages)
    processed = sum(row.status == "processed" for row in messages)
    needs_attention = sum(row.status == "needs_attention" for row in messages)
    skipped_by_reason = Counter(
        row.filter_reason for row in messages if row.filter_reason
    )
    latest_error = next(
        (
            row.failure_reason
            for row in sorted(
                messages,
                key=lambda item: (
                    item.created_at.timestamp()
                    if isinstance(item.created_at, datetime)
                    else 0
                ),
                reverse=True,
            )
            if row.status in {"failed", "failed_final"} and row.failure_reason
        ),
        None,
    )
    last_result = state.last_result if state and state.last_result else {}
    last_error = last_result.get("message") or latest_error
    return {
        "enabled": (
            os.getenv("EMAIL_POLL_ENABLED", "false").lower() == "true"
            and os.getenv("EMAIL_PROVIDER", "imap").lower() == "imap"
        ),
        "last_uid": state.last_uid if state else None,
        "uidvalidity": state.uidvalidity if state else None,
        "last_run_at": last_run.isoformat() if last_run else None,
        "next_poll_at": (last_run + timedelta(seconds=interval)).isoformat()
        if last_run
        else None,
        "last_error": last_error,
        "counts": {
            "processed": processed,
            "failed": failures,
            "needs_attention": needs_attention,
            "skipped_by_reason": dict(skipped_by_reason),
        },
        "last_result": state.last_result
        if state and state.last_result
        else {
            "emails": 0,
            "attachments": 0,
            "duplicates": 0,
            "skipped_known": 0,
            "filtered": 0,
            "failed": 0,
            "filtered_by_reason": {},
        },
        "filtered_by_reason": (
            state.last_result.get("filtered_by_reason", {})
            if state and state.last_result
            else {}
        ),
    }


@router.get("/messages")
def email_poll_messages(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user: dict = Depends(require_role("admin")),
):
    rows = (
        db.query(EmailMessage)
        .order_by(EmailMessage.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": str(row.id),
            "message_id": row.message_id,
            "uid": row.uid,
            "sender": row.sender,
            "subject": row.subject,
            "status": row.status,
            "filter_reason": row.filter_reason,
            "failure_reason": row.failure_reason,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "received_at": row.received_at.isoformat() if row.received_at else None,
            "retryable": row.status == "failed_final",
        }
        for row in rows
    ]


@router.post("/messages/{message_id}/retry", status_code=202)
def retry_email_message(
    message_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("admin")),
):
    row = (
        db.query(EmailMessage)
        .filter(EmailMessage.id == message_id)
        .with_for_update()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Email message not found")
    if row.status != "failed_final":
        raise HTTPException(status_code=409, detail="Only failed emails can be retried")
    row.status = "failed"
    row.attempts = 0
    row.next_retry_at = None
    row.failure_reason = None
    db.add(
        AuditLog(
            action="email_retry_requested",
            entity_type="email",
            entity_id=row.id,
            result="success",
            message="Administrator requested email reprocessing",
            created_by=UUID(str(current_user["sub"]))
            if current_user.get("sub")
            else None,
        )
    )
    db.commit()
    if os.getenv("USE_CELERY", "false").lower() == "true":
        try:
            from src.worker.tasks import poll_email

            poll_email.delay()
        except Exception:
            # The persisted retry remains eligible for the next scheduled/manual poll.
            pass
    documents = (
        db.query(Document)
        .filter(
            Document.email_message_id == row.message_id, Document.status == "failed"
        )
        .all()
    )
    for document in documents:
        document.status = "received"
        document.failure_message = None
        from src.api.rest.routers.document_routers import _process_in_background

        dispatch_document_processing(
            background_tasks, document.id, _process_in_background
        )
    db.commit()
    return {"id": str(row.id), "status": row.status, "documents_queued": len(documents)}
