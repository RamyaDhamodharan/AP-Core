"""Admin endpoints for on-demand email polling and poll status."""

import os

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.rest.dependencies import require_role
from src.db.session_db import get_db
from src.models.email_models import EmailMessage, EmailPollState
from src.services.email_polling_services import poll_email_with_lock

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
    return {
        "enabled": (
            os.getenv("EMAIL_POLL_ENABLED", "false").lower() == "true"
            and os.getenv("EMAIL_PROVIDER", "imap").lower() == "imap"
        ),
        "last_uid": state.last_uid if state else None,
        "uidvalidity": state.uidvalidity if state else None,
        "last_run_at": state.last_run_at.isoformat()
        if state and state.last_run_at
        else None,
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
        }
        for row in rows
    ]
