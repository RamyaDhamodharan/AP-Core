"""Admin endpoints for on-demand email polling and poll status."""

import ast
import os

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.api.rest.dependencies import require_role
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
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
    last = (
        db.query(AuditLog)
        .filter(AuditLog.action == "email_poll_run")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    try:
        result = (
            ast.literal_eval(last.technical_detail)
            if last and last.technical_detail
            else None
        )
    except (ValueError, SyntaxError):
        result = last.technical_detail if last else None
    return {
        "enabled": os.getenv("EMAIL_POLL_ENABLED", "false").lower() == "true",
        "last_run_at": last.created_at.isoformat() if last else None,
        "last_result": result,
    }
