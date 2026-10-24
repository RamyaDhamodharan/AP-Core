"""
Celery tasks for the AP document pipeline.
Flow: background task entry points for parse, email, and follow-up.
Called by: src/api/rest/routers/email_polling_routers.py,
src/services/document_recovery_services.py, src/worker/dispatch.py.
Calls: celery_app, dispatch, document_recovery_services, document_retry_services,
email_models.
Tables: email_messages.
"""

import uuid

from src.worker.celery_app import celery_app


@celery_app.task(
    name="ap.process_document",
    bind=True,
    max_retries=3,
    reject_on_worker_lost=False,
)
def process_document(self, document_id: str) -> None:
    """
    Parse, classify and extract one document (idempotent).
    Inputs: document_id (str).
    Output: None.
    Effects: may queue or retry background work.
    Results: no workflow status or reason code directly.
    """
    from src.services.document_retry_services import (
        classify_document_failure,
        process_document_attempt,
        retry_delay_seconds,
    )

    # Step 1: Delegate idempotency and row locking to the shared retry service.
    try:
        delay = process_document_attempt(uuid.UUID(document_id))
    except Exception as exc:
        if self.request.retries >= 3 or classify_document_failure(exc) != "retryable":
            raise
        delay = retry_delay_seconds(self.request.retries)
        raise self.retry(exc=exc, countdown=delay, max_retries=3) from exc
    # Step 2: Queue only the retry delay returned after a retryable failure.
    if delay is not None:
        raise self.retry(countdown=delay, max_retries=3)


@celery_app.task(name="ap.recover_raw_email", bind=True, max_retries=0)
def recover_raw_email(self, document_id: str) -> None:
    """
    Recover supported attachments from a persisted raw EML document.
    Inputs: document_id (str).
    Output: None.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    from src.services.document_recovery_services import recover_raw_email_document

    recover_raw_email_document(uuid.UUID(document_id))


@celery_app.task(name="ap.send_notification", bind=True, max_retries=2)
def send_notification(self, notification_id: str) -> None:
    """
    Process one queued notification through the shared email outbox.
    Inputs: notification_id (str).
    Output: None.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    from src.worker.dispatch import _send_notification_in_process

    _send_notification_in_process(uuid.UUID(notification_id))


@celery_app.task(name="ap.poll_email", bind=True, max_retries=0)
def poll_email(self) -> dict:
    """
    Run one guarded email poll without allowing overlap across workers.
    Inputs: no explicit inputs.
    Output: dict.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    from src.db.session_db import SessionLocal
    from src.services.email_polling_services import poll_email_with_lock

    db = SessionLocal()
    try:
        return poll_email_with_lock(db)
    finally:
        db.close()


@celery_app.task(name="ap.retry_email_message", bind=True, max_retries=0)
def retry_email_message(self, email_message_id: str) -> dict:
    """
    Re-fetch one explicitly selected mail, including UIDs outside poll overlap.
    Inputs: email_message_id (str).
    Output: dict.
    Effects: reads database rows.
    Results: statuses failed.
    """
    from src.db.session_db import SessionLocal
    from src.models.email_models import EmailMessage
    from src.services.email_polling_services import poll_email_with_lock

    db = SessionLocal()
    try:
        row = db.query(EmailMessage).filter_by(id=uuid.UUID(email_message_id)).first()
        if row is None:
            return {"failed": 1, "message": "Email message no longer exists"}
        return poll_email_with_lock(db, force_uids={row.uid})
    finally:
        db.close()


@celery_app.task(name="ap.mark_overdue", bind=True, max_retries=0)
def mark_overdue(self) -> int:
    """
    Move approved unpaid invoices past their local due date into overdue.
    Inputs: no explicit inputs.
    Output: int.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    from src.db.session_db import SessionLocal
    from src.services.follow_up_services import mark_overdue as mark_overdue_service

    db = SessionLocal()
    try:
        return mark_overdue_service(db)
    finally:
        db.close()


@celery_app.task(name="ap.run_follow_ups", bind=True, max_retries=0)
def run_follow_ups(self) -> dict:
    """
    Process due internal reminders and create vendor drafts as needed.
    Inputs: no explicit inputs.
    Output: dict.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    from src.db.session_db import SessionLocal
    from src.services.follow_up_services import run_follow_ups as run_follow_ups_service

    db = SessionLocal()
    try:
        return run_follow_ups_service(db)
    finally:
        db.close()


@celery_app.task(name="ap.sweep_stuck_documents", bind=True, max_retries=0)
def sweep_stuck_documents(self) -> int:
    """
    Mark abandoned document work as failed so users can retry it.
    Inputs: no explicit inputs.
    Output: int.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    from src.db.session_db import SessionLocal
    from src.services.document_recovery_services import sweep_stuck_documents as sweep

    # Step 1: Let recovery service persist the failure reason and audit event.
    db = SessionLocal()
    try:
        return sweep(db)
    finally:
        db.close()
