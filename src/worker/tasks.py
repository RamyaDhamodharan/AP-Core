"""Celery tasks for the AP document pipeline."""

import uuid

from src.worker.celery_app import celery_app


@celery_app.task(
    name="ap.process_document",
    bind=True,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def process_document(self, document_id: str) -> None:
    """Parse, classify and extract one document (idempotent)."""
    # Imported lazily so the worker boots without loading the web app.
    from src.api.rest.routers.document_routers import _process_in_background

    _process_in_background(uuid.UUID(document_id))


@celery_app.task(name="ap.send_notification", bind=True, max_retries=2)
def send_notification(self, notification_id: str) -> None:
    """Send one queued notification and retry transient failures at most twice."""
    from src.db.session_db import SessionLocal
    from src.models.notification_models import Notification
    from src.services.notification_services import send_notification as send

    db = SessionLocal()
    should_retry = False
    try:
        notification = db.get(Notification, uuid.UUID(notification_id))
        if notification is None or notification.status == "sent":
            return
        sent = send(notification)
        db.commit()
        should_retry = not sent and notification.attempts < 3
    except Exception as exc:
        db.rollback()
        should_retry = True
        error = exc
    finally:
        db.close()
    if should_retry:
        raise self.retry(
            exc=locals().get("error", RuntimeError("SMTP send failed")),
            countdown=2**self.request.retries,
        )


@celery_app.task(name="ap.poll_email", bind=True, max_retries=0)
def poll_email(self) -> dict:
    """Run one guarded email poll without allowing overlap across workers."""
    from src.db.session_db import SessionLocal
    from src.services.email_polling_services import poll_email_with_lock

    db = SessionLocal()
    try:
        return poll_email_with_lock(db)
    finally:
        db.close()
