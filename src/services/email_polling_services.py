"""IMAP polling and ingestion of vendor invoice email attachments."""

import contextlib
import email
import email.utils
import imaplib
import logging
import os
import time
from datetime import UTC, datetime
from email import policy
from email.message import Message

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.notification_models import Notification
from src.services.document_ingest_services import ingest_file
from src.worker.dispatch import dispatch_queued_notifications

logger = logging.getLogger(__name__)
ATTACHMENT_TYPES = {".pdf", ".png", ".jpg", ".jpeg"}


def poll_email_with_lock(db) -> dict:
    """Run the poll under a shared Redis lock for API and Celery callers."""
    from redis import Redis

    lock = None
    try:
        lock = Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0")
        ).lock("ap:email-poll-lock", timeout=300, blocking_timeout=0)
        if not lock.acquire(blocking=False):
            return {
                "emails": 0,
                "attachments": 0,
                "duplicates": 0,
                "failed": 0,
                "skipped": "lock",
            }
        return poll_email_messages(db)
    except Exception as exc:
        logger.exception("Email poll lock or run failed")
        _audit(
            db,
            "email_failed",
            "Email polling could not start",
            result="error",
            detail=repr(exc),
        )
        db.commit()
        return {"emails": 0, "attachments": 0, "duplicates": 0, "failed": 1}
    finally:
        if lock is not None:
            with contextlib.suppress(Exception):
                lock.release()


def _audit(db, action, message, *, result="success", entity_id=None, detail=None):
    db.add(
        AuditLog(
            action=action,
            entity_type="document",
            entity_id=entity_id,
            result=result,
            message=message,
            technical_detail=detail,
        )
    )


def _metadata(raw: bytes) -> tuple[Message, str | None, str, str, datetime | None]:
    msg = email.message_from_bytes(raw, policy=policy.default)
    message_id = (msg.get("Message-ID") or "").strip() or None
    sender = email.utils.parseaddr(msg.get("From", ""))[1]
    subject = str(msg.get("Subject", ""))[:500]
    received = None
    try:
        parsed = email.utils.parsedate_to_datetime(msg.get("Date", ""))
        received = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError, OverflowError):
        pass
    return msg, message_id, sender, subject, received


def poll_email_messages(db, imap_factory=None, *, sleep=time.sleep) -> dict:
    """Poll unseen messages. Individual failures are audited and isolated."""
    host = os.getenv("IMAP_HOST", "")
    if not host:
        result = {
            "emails": 0,
            "attachments": 0,
            "duplicates": 0,
            "failed": 0,
            "error": "IMAP_HOST is not configured",
        }
        _audit(
            db,
            "email_poll_run",
            "Email polling completed",
            result="warning",
            detail=str(result),
        )
        db.commit()
        return result
    factory = imap_factory or _imap_factory
    counts = {"emails": 0, "attachments": 0, "duplicates": 0, "failed": 0}
    client = None
    last_exc = None
    for attempt in range(3):
        try:
            client = factory(host, int(os.getenv("IMAP_PORT", "993")))
            client.login(os.getenv("IMAP_USER", ""), os.getenv("IMAP_PASSWORD", ""))
            break
        except (OSError, imaplib.IMAP4.error, TimeoutError) as exc:
            last_exc = exc
            if client:
                with contextlib.suppress(Exception):
                    client.logout()
            client = None
            if attempt < 2:
                sleep(2**attempt)
    if client is None:
        detail = repr(last_exc)
        logger.error("IMAP connection failed: %s", detail)
        _audit(
            db,
            "email_failed",
            "Email polling could not connect",
            result="error",
            detail=detail,
        )
        _audit(
            db,
            "email_poll_run",
            "Email polling completed",
            result="error",
            detail=str(counts),
        )
        db.commit()
        counts["failed"] += 1
        return counts

    try:
        status, _ = client.select(os.getenv("IMAP_FOLDER", "INBOX"))
        if status != "OK":
            raise RuntimeError("Could not select IMAP folder")
        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            raise RuntimeError("Could not search unseen IMAP messages")
        numbers = (data[0].split() if data and data[0] else [])[
            -int(os.getenv("EMAIL_POLL_MAX_MESSAGES", "25")) :
        ]
        for number in numbers:
            counts["emails"] += 1
            _process_message(db, client, number, counts)
        _audit(db, "email_poll_run", "Email polling completed", detail=str(counts))
        db.commit()
    except Exception as exc:
        logger.exception("IMAP polling batch failed")
        _audit(
            db,
            "email_failed",
            "Email polling batch failed",
            result="error",
            detail=repr(exc),
        )
        db.commit()
        counts["failed"] += 1
    finally:
        with contextlib.suppress(Exception):
            client.logout()
    return counts


def _imap_factory(host: str, port: int):
    if os.getenv("IMAP_USE_SSL", "true").lower() in {"1", "true", "yes", "on"}:
        return imaplib.IMAP4_SSL(host, port, timeout=30)
    return imaplib.IMAP4(host, port, timeout=30)


def _process_message(db, client, number, counts):
    parent = None
    message_id = None
    try:
        status, response = client.fetch(number, "(RFC822)")
        if status != "OK" or not response:
            raise RuntimeError("Could not fetch email")
        raw = next(item[1] for item in response if isinstance(item, tuple))
        msg, message_id, sender, subject, received = _metadata(raw)
        if message_id:
            prior_failures = (
                db.query(AuditLog)
                .filter(AuditLog.action == "email_failed")
                .filter(AuditLog.technical_detail.contains(message_id))
                .count()
            )
            if prior_failures >= 3:
                _audit(
                    db,
                    "email_failed",
                    "Email reached the retry limit and remains unseen",
                    result="error",
                    detail=message_id,
                )
                db.commit()
                return
        if (
            message_id
            and db.query(Document)
            .filter_by(email_message_id=message_id, parent_document_id=None)
            .first()
        ):
            counts["duplicates"] += 1
            _audit(
                db,
                "email_skipped_duplicate",
                "Email was already ingested",
                detail=message_id,
            )
            db.commit()
            client.store(number, "+FLAGS", "\\Seen")
            return

        meta = {
            "email_sender": sender or None,
            "email_subject": subject or None,
            "email_received_at": received,
            "email_message_id": message_id,
        }
        result = ingest_file(
            db, "email.eml", raw, "message/rfc822", None, "email_poll", email_meta=meta
        )
        if result.status != "received" or result.document_id is None:
            raise RuntimeError(result.message)
        parent = db.get(Document, result.document_id)
        _audit(
            db,
            "email_received",
            "Email received for processing",
            entity_id=parent.id,
            detail=message_id,
        )
        db.commit()  # Parent row is durable before the server marks the message Seen.

        valid = 0
        for part in msg.walk():
            if part.is_multipart():
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            ext = os.path.splitext(filename or "")[1].lower()
            if not filename or ext not in ATTACHMENT_TYPES:
                if part.get_content_maintype() == "image" and len(payload) < 5 * 1024:
                    continue
                if filename:
                    logger.info("Ignoring unsupported email attachment %s", filename)
                continue
            attachment = ingest_file(
                db,
                filename,
                payload,
                part.get_content_type(),
                None,
                "email_poll",
                parent_document_id=parent.id,
            )
            if attachment.status == "received":
                valid += 1
                counts["attachments"] += 1
                _audit(
                    db,
                    "attachment_ingested",
                    "Email attachment ingested",
                    entity_id=attachment.document_id,
                    detail=filename,
                )
            else:
                counts["failed"] += 1
                _audit(
                    db,
                    "email_failed",
                    "Email attachment could not be ingested",
                    result="error",
                    entity_id=parent.id,
                    detail=f"{filename}: {attachment.message}",
                )
            db.commit()

        if not valid:
            parent.status = "ignored"
            parent.failure_message = "No bill attachment found"
            allowed = {
                item.strip().casefold()
                for item in os.getenv("TEST_MAILBOX_ALLOWLIST", "").split(",")
                if item.strip()
            }
            if sender and sender.casefold() in allowed:
                db.add(
                    Notification(
                        document_id=parent.id,
                        recipient_email=sender,
                        subject="No bill attachment found",
                        body=(
                            "We could not find a supported bill attachment "
                            "in your email."
                        ),
                        status="queued",
                    )
                )
                db.commit()
                dispatch_queued_notifications(db, parent.id)
            _audit(
                db,
                "email_failed",
                "No bill attachment found",
                result="warning",
                entity_id=parent.id,
            )
        db.commit()
        client.store(number, "+FLAGS", "\\Seen")
    except Exception as exc:
        db.rollback()
        logger.exception("Failed to process IMAP message %s", number)
        counts["failed"] += 1
        _audit(
            db,
            "email_failed",
            "Email could not be processed and will be retried",
            result="error",
            entity_id=getattr(parent, "id", None),
            detail=f"{message_id or number}: {exc!r}",
        )
        db.commit()
