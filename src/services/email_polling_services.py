"""Read-only Gmail IMAP polling with UID checkpoints and safe attachment intake."""

import contextlib
import email
import email.utils
import hashlib
import html.parser
import imaplib
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import Message
from pathlib import PurePath, PureWindowsPath

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_models import EmailMessage, EmailPollState
from src.models.notification_models import Notification
from src.services.document_ingest_services import ingest_file

logger = logging.getLogger(__name__)
OVERLAP_UIDS = 10
MAX_ATTEMPTS = 3
MAX_ATTACHMENTS_PER_EMAIL = 10
INLINE_IMAGE_MIN_BYTES = 5 * 1024
IMAP_AUTH_ERROR = "Gmail rejected the login: use an App Password and enable IMAP"
IMAP_REACHABILITY_ERROR = "Cannot reach IMAP host"
IMAP_CONFIG_ERROR = "IMAP_USER / IMAP_PASSWORD not set"
EMPTY_COUNTS = {
    "emails": 0,
    "attachments": 0,
    "duplicates": 0,
    "filtered": 0,
    "failed": 0,
}
ATTACHMENT_FORMATS = {
    "application/pdf": (".pdf", lambda data: data.startswith(b"%PDF-")),
    "image/png": (".png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/jpeg": (".jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
    "image/jpg": (".jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
}
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|"
    r"imap_password\s*[:=]\s*|"
    r"(?:password|passwd|token|access_token|refresh_token|secret|api[_-]?key)\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_URL_CREDENTIALS = re.compile(r"(?i)(://[^:/\s]+:)[^@/\s]+(@)")


def _sanitize_exception_message(message: str) -> str:
    message = _SENSITIVE_ASSIGNMENT.sub(r"\1[REDACTED]", str(message))
    message = _URL_CREDENTIALS.sub(r"\1[REDACTED]\2", message)
    for name in (
        "IMAP_PASSWORD",
        "MINIO_SECRET_KEY",
        "JWT_SECRET_KEY",
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
    ):
        secret = os.getenv(name, "")
        if secret and len(secret) >= 4:
            message = message.replace(secret, "[REDACTED]")
    return message[:500]


def _email_exception_detail(uid: int, exc: Exception) -> str:
    message = _sanitize_exception_message(str(exc)) or "No exception message"
    return f"UID {uid}: {type(exc).__name__}: {message}"[:500]


def _log_email_exception(uid: int, exc: Exception) -> None:
    safe_exception = RuntimeError(
        f"{type(exc).__name__}: "
        f"{_sanitize_exception_message(str(exc)) or 'No exception message'}"
    )
    logger.exception(
        "Email processing failed for UID %s",
        uid,
        exc_info=(RuntimeError, safe_exception, exc.__traceback__),
    )


def _friendly_imap_error(exc: Exception) -> str:
    if "AUTHENTICATIONFAILED" in str(exc).upper():
        return IMAP_AUTH_ERROR
    return IMAP_REACHABILITY_ERROR


def _audit(db, action, message, *, result="success", entity_id=None, detail=None):
    db.add(
        AuditLog(
            action=action,
            entity_type="email",
            entity_id=entity_id,
            result=result,
            message=message,
            technical_detail=detail,
        )
    )


class _HTMLText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.casefold() in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag.casefold() in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def _metadata(raw: bytes) -> tuple[Message, str, str, str, datetime | None, str]:
    msg = email.message_from_bytes(raw, policy=policy.default)
    message_id = str(msg.get("Message-ID", "")).strip()
    sender = email.utils.parseaddr(str(msg.get("From", "")))[1].strip()
    subject = str(msg.get("Subject", ""))[:500]
    received = None
    try:
        parsed = email.utils.parsedate_to_datetime(str(msg.get("Date", "")))
        received = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError, OverflowError):
        pass
    if not message_id:
        message_id = f"<raw-{hashlib.sha256(raw).hexdigest()}@local>"

    plain_parts = []
    html_parts = []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_filename():
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, TypeError):
            continue
        if not isinstance(content, str):
            continue
        if part.get_content_type() == "text/plain":
            plain_parts.append(content)
        elif part.get_content_type() == "text/html":
            html_parts.append(content)
    if plain_parts:
        body = "\n".join(plain_parts)
    else:
        parser = _HTMLText()
        for content in html_parts:
            parser.feed(content)
        body = " ".join(" ".join(parser.parts).split())
    return msg, message_id, sender, subject, received, body[:2000]


def _uidvalidity(client) -> int:
    response = client.response("UIDVALIDITY")
    values = response[1] if response else []
    if not values:
        raise RuntimeError("IMAP UIDVALIDITY was unavailable")
    value = values[0]
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="strict")
    return int(value)


def _uid_search(client, criteria: str) -> list[int]:
    status, data = client.uid("search", None, criteria)
    if status != "OK":
        raise RuntimeError("Could not search IMAP UIDs")
    values = data[0].split() if data and data[0] else []
    return sorted({int(value) for value in values})


def _internal_date(response) -> datetime | None:
    for item in response:
        if not isinstance(item, tuple) or not item:
            continue
        header = item[0]
        if isinstance(header, bytes):
            header = header.decode("ascii", errors="ignore")
        match = re.search(r'INTERNALDATE\s+"([^"]+)"', str(header), re.IGNORECASE)
        if not match:
            continue
        try:
            value = email.utils.parsedate_to_datetime(match.group(1))
        except (TypeError, ValueError, OverflowError):
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return None


def _count_result(counts: dict) -> dict:
    return {key: int(counts.get(key, 0)) for key in EMPTY_COUNTS}


def _record_run(
    db,
    state,
    counts: dict,
    *,
    action="email_poll_run",
    message="Email polling completed",
    audit_result="success",
    audit_detail=None,
):
    result = _count_result(counts)
    now = datetime.now(UTC)
    state.last_run_at = now
    state.last_result = result
    state.updated_at = now
    _audit(
        db,
        action,
        message,
        result=audit_result,
        detail=audit_detail or str(result),
    )
    db.commit()
    return result


def poll_email_with_lock(db) -> dict:
    """Run the poll under the existing shared Redis lock."""
    from redis import Redis

    lock = None
    try:
        lock = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0")).lock(
            "ap:email-poll-lock", timeout=300, blocking_timeout=0
        )
        if not lock.acquire(blocking=False):
            return {**EMPTY_COUNTS, "skipped": "lock"}
        return poll_email_messages(db)
    except Exception:
        logger.warning("Email polling Redis lock is unavailable")
        _audit(
            db,
            "email_failed",
            "Email polling could not start",
            result="error",
            detail="Redis lock unavailable",
        )
        db.commit()
        return {
            **EMPTY_COUNTS,
            "failed": 1,
            "message": "Email polling lock unavailable",
        }
    finally:
        if lock is not None:
            with contextlib.suppress(Exception):
                lock.release()


def poll_email_messages(db, imap_factory=None, *, sleep=time.sleep) -> dict:
    """Fetch new UIDs read-only, committing each parent before its checkpoint."""
    host = os.getenv("IMAP_HOST", "").strip()
    username = os.getenv("IMAP_USER", "").strip()
    password = os.getenv("IMAP_PASSWORD", "")
    if not username or not password:
        return _poll_configuration_failure(db, IMAP_CONFIG_ERROR)
    if not host:
        return _poll_configuration_failure(db, IMAP_REACHABILITY_ERROR)

    factory = imap_factory or _imap_factory
    client = None
    last_exc = None
    for attempt in range(3):
        try:
            client = factory(host, int(os.getenv("IMAP_PORT", "993")))
            client.login(username, password)
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
        return _poll_configuration_failure(
            db, _friendly_imap_error(last_exc or RuntimeError())
        )

    mailbox = username
    folder = os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX"
    counts = dict(EMPTY_COUNTS)
    state = None
    try:
        status, _ = client.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError("Could not select IMAP folder")
        validity = _uidvalidity(client)
        state = (
            db.query(EmailPollState)
            .filter_by(mailbox=mailbox, folder=folder)
            .with_for_update()
            .first()
        )
        all_uids = _uid_search(client, "ALL")
        highest_uid = max(all_uids, default=0)
        initial_cutoff = None
        # uidvalidity=0 is the placeholder created when IMAP was unavailable
        # before the mailbox could be inspected. Treat the next successful
        # connection as the real first run and establish a baseline checkpoint.
        initial_run = state is None or state.uidvalidity == 0

        max_messages = max(1, int(os.getenv("EMAIL_POLL_MAX_MESSAGES", "25")))
        if initial_run:
            if state is None:
                state = EmailPollState(
                    mailbox=mailbox,
                    folder=folder,
                    uidvalidity=validity,
                    last_uid=highest_uid,
                )
                db.add(state)
            else:
                state.uidvalidity = validity
                state.last_uid = highest_uid
            lookback_hours = max(
                0, int(os.getenv("EMAIL_POLL_INITIAL_LOOKBACK_HOURS", "0"))
            )
            if lookback_hours <= 0 or not all_uids:
                return _record_run(
                    db,
                    state,
                    counts,
                    message="Email polling initialized without backfill",
                )
            initial_cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
            since = initial_cutoff.strftime("%d-%b-%Y")
            uids = _uid_search(client, f"SINCE {since}")
            if not uids:
                return _record_run(
                    db,
                    state,
                    counts,
                    message="Email polling initialized without matching lookback mail",
                )
            state.last_uid = max(0, min(uids) - 1)
            uids = uids[:max_messages]
            db.commit()
        elif state.uidvalidity != validity:
            state.uidvalidity = validity
            state.last_uid = 0
            db.commit()
            _audit(
                db,
                "email_poll_run",
                "Email UIDVALIDITY changed; checkpoint reset",
                detail="Message-ID deduplication protects replayed messages",
            )
            db.commit()
            uids = _uid_search(client, "UID 1:*")
            uids = uids[:max_messages]
        else:
            new_uids = _uid_search(client, f"UID {state.last_uid + 1}:*")
            new_uids = new_uids[:max_messages]
            overlap_start = max(1, state.last_uid - OVERLAP_UIDS + 1)
            overlap_uids = (
                _uid_search(client, f"UID {overlap_start}:{state.last_uid}")
                if state.last_uid > 0
                else []
            )
            uids = sorted(set(overlap_uids + new_uids))

        uids = [uid for uid in sorted(set(uids)) if uid <= highest_uid]
        blocked_by_failure = False

        for uid in uids:
            counts["emails"] += 1
            was_new_uid = uid > state.last_uid
            try:
                safe_to_advance = _process_uid(
                    db,
                    client,
                    mailbox,
                    folder,
                    uid,
                    counts,
                    received_after=initial_cutoff if initial_run else None,
                )
            except Exception as exc:
                db.rollback()
                _log_email_exception(uid, exc)
                safe_to_advance = _record_uid_fetch_failure(
                    db, mailbox, folder, uid, counts, exc
                )
            if was_new_uid and not safe_to_advance:
                blocked_by_failure = True
            if was_new_uid and safe_to_advance and not blocked_by_failure:
                state.last_uid = uid
                state.updated_at = datetime.now(UTC)
                db.commit()

        return _record_run(db, state, counts)
    except Exception as exc:
        friendly = _friendly_imap_error(exc)
        logger.warning("IMAP polling batch failed: %s", friendly)
        db.rollback()
        counts["failed"] += 1
        if state is None and mailbox:
            state = (
                db.query(EmailPollState)
                .filter_by(mailbox=mailbox, folder=folder)
                .first()
            )
        if state is not None:
            _record_run(
                db,
                state,
                counts,
                action="email_failed",
                message="Email polling batch failed",
                audit_result="error",
                audit_detail=friendly,
            )
        else:
            _audit(
                db,
                "email_failed",
                "Email polling batch failed",
                result="error",
                detail=friendly,
            )
            db.commit()
        return {**_count_result(counts), "message": friendly}
    finally:
        with contextlib.suppress(Exception):
            client.logout()


def _poll_configuration_failure(db, message: str) -> dict:
    logger.warning("Email polling unavailable: %s", message)
    mailbox = os.getenv("IMAP_USER", "").strip()
    folder = os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX"
    counts = {**EMPTY_COUNTS, "failed": 1}
    if mailbox:
        state = (
            db.query(EmailPollState).filter_by(mailbox=mailbox, folder=folder).first()
        )
        if state is None:
            state = EmailPollState(
                mailbox=mailbox,
                folder=folder,
                uidvalidity=0,
                last_uid=0,
            )
            db.add(state)
        _record_run(
            db,
            state,
            counts,
            action="email_failed",
            message="Email polling could not start",
            audit_result="error",
            audit_detail=message,
        )
    else:
        _audit(
            db,
            "email_failed",
            "Email polling could not start",
            result="error",
            detail=message,
        )
        db.commit()
    return {**_count_result(counts), "message": message}


def _imap_factory(host: str, port: int):
    if os.getenv("IMAP_USE_SSL", "true").lower() in {"1", "true", "yes", "on"}:
        return imaplib.IMAP4_SSL(host, port, timeout=30)
    return imaplib.IMAP4(host, port, timeout=30)


def _sender_filter_reason(msg: Message, mailbox: str, sender: str) -> str | None:
    sender_key = sender.casefold()
    mailbox_key = email.utils.parseaddr(mailbox)[1].casefold() or mailbox.casefold()
    if sender_key and sender_key == mailbox_key:
        return "sender_is_mailbox"
    auto_header = msg.get("Auto-Submitted")
    if auto_header is not None and str(auto_header).strip().casefold() != "no":
        return "auto_submitted"
    if str(msg.get("Precedence", "")).strip().casefold() in {"bulk", "junk"}:
        return "precedence_bulk_or_junk"
    subject = str(msg.get("Subject", "")).casefold()
    bounce_sender = (
        sender_key == "mailer-daemon"
        or sender_key == "postmaster"
        or sender_key.startswith("mailer-daemon@")
        or sender_key.startswith("postmaster@")
        or str(msg.get("Return-Path", "")).strip() == "<>"
    )
    bounce_subject = any(
        phrase in subject
        for phrase in (
            "delivery status notification",
            "undelivered mail",
            "returned mail",
            "failure notice",
            "mail delivery failed",
        )
    )
    if bounce_sender or bounce_subject:
        return "bounce_or_mailer_daemon"
    allowed = _address_list("EMAIL_ALLOWED_SENDERS")
    blocked = _address_list("EMAIL_BLOCKED_SENDERS")
    if sender_key in blocked:
        return "blocked_sender"
    if allowed and sender_key not in allowed:
        return "sender_not_allowlisted"
    return None


def _address_list(variable: str) -> set[str]:
    return {
        item.strip().casefold()
        for item in os.getenv(variable, "").split(",")
        if item.strip()
    }


def _safe_filename(filename: str, fallback_extension: str) -> str:
    name = PureWindowsPath(filename).name
    name = PurePath(name).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = f"attachment{fallback_extension}"
    if PurePath(name).suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg"}:
        stem = PurePath(name).stem or "attachment"
        name = f"{stem}{fallback_extension}"
    return name[:180]


def _attachment_format(part: Message, payload: bytes) -> tuple[str, str] | None:
    content_type = part.get_content_type().casefold()
    expected = ATTACHMENT_FORMATS.get(content_type)
    if expected is None:
        return None
    extension, check_magic = expected
    if not check_magic(payload):
        return None
    filename = part.get_filename() or f"attachment{extension}"
    suffix = PurePath(PureWindowsPath(filename).name).suffix.casefold()
    expected_extensions = {"image/jpeg": {".jpg", ".jpeg"}}.get(
        content_type, {extension}
    )
    if suffix and suffix not in expected_extensions:
        return None
    return extension, filename


def _record_failed_attachment(
    db, parent: Document, filename: str, content_type: str, payload: bytes, message: str
):
    digest = hashlib.sha256(payload).hexdigest()
    existing = db.query(Document).filter_by(file_hash=digest).first()
    if existing:
        return
    db.add(
        Document(
            id=uuid.uuid4(),
            original_filename=_safe_filename(filename, ".pdf"),
            file_hash=digest,
            sha256=digest,
            mime=content_type,
            size=len(payload),
            status="failed",
            failure_message=message[:500],
            parent_document_id=parent.id,
            source="email_poll",
        )
    )
    db.commit()


def _process_uid(
    db,
    client,
    mailbox: str,
    folder: str,
    uid: int,
    counts: dict,
    *,
    received_after: datetime | None = None,
) -> bool:
    """Return true only when this UID is committed or has exhausted retries."""
    try:
        status, response = client.uid("fetch", str(uid), "(INTERNALDATE BODY.PEEK[])")
    except Exception as exc:
        _log_email_exception(uid, exc)
        return _record_uid_fetch_failure(db, mailbox, folder, uid, counts, exc)
    if status != "OK" or not response:
        exc = RuntimeError("IMAP UID fetch failed")
        _log_email_exception(uid, exc)
        return _record_uid_fetch_failure(db, mailbox, folder, uid, counts, exc)
    raw = next((item[1] for item in response if isinstance(item, tuple)), None)
    if not isinstance(raw, bytes):
        exc = RuntimeError("IMAP UID had no message body")
        _log_email_exception(uid, exc)
        return _record_uid_fetch_failure(db, mailbox, folder, uid, counts, exc)

    server_received = _internal_date(response)
    msg, message_id, sender, subject, received, preview = _metadata(raw)
    received = server_received or received
    prior_by_id = db.query(EmailMessage).filter_by(message_id=message_id).first()
    prior_by_uid = (
        db.query(EmailMessage)
        .filter_by(mailbox=mailbox, folder=folder, uid=uid)
        .first()
    )
    prior = prior_by_id or prior_by_uid
    if prior and prior.status in {"processed", "ignored", "failed_final"}:
        if prior.status != "failed_final":
            counts["duplicates"] += 1
            _audit(
                db,
                "email_skipped_duplicate",
                "Email was already ingested",
                detail=hashlib.sha256(message_id.encode()).hexdigest()[:16],
            )
        db.commit()
        return True

    attempt_no = (prior.attempts if prior else 0) + 1
    date_for_key = received or datetime.now(UTC)
    message_hash = hashlib.sha1(message_id.encode()).hexdigest()
    eml_key = f"emails/{date_for_key:%Y/%m}/{message_hash}.eml"
    if prior is None:
        prior = EmailMessage(
            message_id=message_id,
            uid=uid,
            mailbox=mailbox,
            folder=folder,
            sender=sender or None,
            subject=subject or None,
            received_at=received,
            body_preview=preview,
            raw_object_key=eml_key,
            status="processing",
            attempts=attempt_no,
        )
        db.add(prior)
    else:
        prior.message_id = message_id
        prior.uid = uid
        prior.folder = folder
        prior.sender = sender or None
        prior.subject = subject or None
        prior.received_at = received
        prior.body_preview = preview
        prior.raw_object_key = eml_key
        prior.status = "processing"
        prior.attempts = attempt_no
    db.commit()

    if received_after and (received is None or received < received_after):
        prior.status = "ignored"
        _audit(
            db,
            "email_skipped_filtered",
            "Message is older than the configured initial lookback",
            entity_id=prior.id,
            detail="outside_initial_lookback",
        )
        db.commit()
        counts["filtered"] += 1
        return True

    filter_reason = _sender_filter_reason(msg, mailbox, sender)
    if filter_reason:
        prior.status = "ignored"
        _audit(
            db,
            "email_skipped_filtered",
            "Email was skipped by configured sender or automation filters",
            entity_id=prior.id,
            detail=filter_reason,
        )
        db.commit()
        counts["filtered"] += 1
        return True

    try:
        root = (
            db.query(Document)
            .filter_by(email_message_id=message_id, parent_document_id=None)
            .first()
        )
        if root is None:
            result = ingest_file(
                db,
                "email.eml",
                raw,
                "message/rfc822",
                None,
                "email_poll",
                email_meta={
                    "email_sender": sender or None,
                    "email_subject": subject or None,
                    "email_received_at": received,
                    "email_message_id": message_id,
                    "email_uid": uid,
                    "email_body_preview": preview,
                    "email_eml_key": eml_key,
                },
                object_key=eml_key,
            )
            if result.status != "received" or result.document_id is None:
                raise RuntimeError(
                    "Raw email document could not be persisted: "
                    f"{result.message}"
                )
            root = db.get(Document, result.document_id)
        if root is None:
            raise RuntimeError("Raw email document was unavailable after commit")

        _audit(
            db,
            "email_received",
            "Email received for processing",
            entity_id=prior.id,
            detail=hashlib.sha256(message_id.encode()).hexdigest()[:16],
        )
        db.commit()
        valid = 0
        attachment_failure = False
        accepted_attachment_count = 0
        max_bytes = (
            max(1, int(os.getenv("EMAIL_MAX_ATTACHMENT_MB", "20"))) * 1024 * 1024
        )
        received_at = received or datetime.now(UTC)

        for part in msg.walk():
            if part.is_multipart():
                continue
            filename = part.get_filename()
            disposition = part.get_content_disposition()
            is_candidate = bool(filename) or disposition in {"attachment", "inline"}
            if not is_candidate:
                continue
            payload = part.get_payload(decode=True) or b""
            content_type = part.get_content_type().casefold()
            if (
                disposition == "inline"
                and content_type.startswith("image/")
                and len(payload) < INLINE_IMAGE_MIN_BYTES
            ):
                continue
            if accepted_attachment_count >= MAX_ATTACHMENTS_PER_EMAIL:
                counts["failed"] += 1
                _audit(
                    db,
                    "email_failed",
                    "Attachment limit exceeded",
                    result="error",
                    entity_id=prior.id,
                    detail="More than 10 attachments",
                )
                continue
            accepted_attachment_count += 1
            original_name = filename or "attachment"
            if len(payload) > max_bytes:
                message = (
                    f"Attachment exceeds the {max_bytes // (1024 * 1024)} MB limit"
                )
                _record_failed_attachment(
                    db, root, original_name, content_type, payload, message
                )
                counts["failed"] += 1
                _audit(
                    db,
                    "email_failed",
                    "Email attachment exceeded the configured size limit",
                    result="error",
                    entity_id=prior.id,
                    detail="attachment_too_large",
                )
                continue
            detected = _attachment_format(part, payload)
            if detected is None:
                _record_failed_attachment(
                    db,
                    root,
                    original_name,
                    content_type,
                    payload,
                    "Attachment type or file signature is not an allowed bill format",
                )
                counts["failed"] += 1
                _audit(
                    db,
                    "email_failed",
                    "Email attachment type or content did not match",
                    result="error",
                    entity_id=prior.id,
                    detail="attachment_type_or_magic_mismatch",
                )
                continue

            extension, _ = detected
            safe_name = _safe_filename(original_name, extension)
            # Normalize a missing extension to the verified media format.
            suffix = PurePath(safe_name).suffix.lower()
            if not suffix:
                safe_name = f"{safe_name}{extension}"
            digest = hashlib.sha256(payload).hexdigest()
            duplicate = db.query(Document).filter_by(file_hash=digest).first()
            if duplicate:
                counts["duplicates"] += 1
                valid += 1
                continue
            document_id = uuid.uuid4()
            object_key = f"incoming/{received_at:%Y/%m}/{document_id}/{safe_name}"
            result = ingest_file(
                db,
                safe_name,
                payload,
                content_type,
                None,
                "email_poll",
                parent_document_id=root.id,
                object_key=object_key,
                document_id=document_id,
            )
            if result.status == "received":
                valid += 1
                counts["attachments"] += 1
                _audit(
                    db,
                    "attachment_ingested",
                    "Email attachment ingested",
                    entity_id=result.document_id,
                    detail=digest[:16],
                )
                db.commit()
            elif result.status == "duplicate_blocked":
                counts["duplicates"] += 1
                valid += 1
            else:
                attachment_failure = True
                counts["failed"] += 1
                _audit(
                    db,
                    "email_failed",
                    "Email attachment could not be ingested",
                    result="error",
                    entity_id=prior.id,
                    detail="attachment_ingestion_failed",
                )

        if not valid:
            root.status = "ignored"
            root.failure_message = "No bill attachment found"
            _audit(
                db,
                "email_ignored",
                "No bill attachment found",
                entity_id=prior.id,
            )
            _queue_no_attachment_notification(db, root, sender)
        if attachment_failure:
            prior.status = "failed_final" if attempt_no >= MAX_ATTEMPTS else "failed"
            _audit(
                db,
                "email_failed",
                "Email processing failed",
                result="error",
                entity_id=prior.id,
                detail=f"UID {uid}: attachment ingestion failed",
            )
        else:
            prior.status = "processed" if valid else "ignored"
        db.commit()
        if not valid:
            _dispatch_queued_notifications(db, root.id)
        return not attachment_failure or attempt_no >= MAX_ATTEMPTS
    except Exception as exc:
        _log_email_exception(uid, exc)
        db.rollback()
        retry_row = db.query(EmailMessage).filter_by(message_id=message_id).first()
        if retry_row is None:
            retry_row = EmailMessage(
                message_id=message_id,
                uid=uid,
                mailbox=mailbox,
                folder=folder,
                raw_object_key=eml_key,
                status="failed",
                attempts=attempt_no,
            )
            db.add(retry_row)
        retry_row.attempts = attempt_no
        retry_row.status = "failed_final" if attempt_no >= MAX_ATTEMPTS else "failed"
        _audit(
            db,
            "email_failed",
            "Email processing failed",
            result="error",
            entity_id=retry_row.id,
            detail=_email_exception_detail(uid, exc),
        )
        db.commit()
        counts["failed"] += 1
        return attempt_no >= MAX_ATTEMPTS


def _queue_no_attachment_notification(db, root: Document, sender: str) -> None:
    if not sender:
        return
    existing = (
        db.query(Notification)
        .filter_by(
            document_id=root.id,
            recipient_email=sender,
            subject="No bill attachment found",
        )
        .first()
    )
    if existing:
        return
    db.add(
        Notification(
            document_id=root.id,
            recipient_email=sender,
            subject="No bill attachment found",
            body=(
                "No bill attachment found. Please resend with a PDF, PNG, or "
                "JPEG attachment."
            ),
            status="queued",
        )
    )
    db.commit()


def _dispatch_queued_notifications(db, document_id) -> None:
    from src.worker.dispatch import dispatch_queued_notifications

    dispatch_queued_notifications(db, document_id)


def _record_uid_fetch_failure(
    db, mailbox: str, folder: str, uid: int, counts: dict, exc: Exception
) -> bool:
    """Persist fetch attempts by mailbox/folder/UID without exposing IMAP errors."""
    row = (
        db.query(EmailMessage)
        .filter_by(mailbox=mailbox, folder=folder, uid=uid)
        .first()
    )
    attempt_no = (row.attempts if row else 0) + 1
    if row is None:
        key_digest = hashlib.sha1(f"{mailbox}:{folder}:{uid}".encode()).hexdigest()
        row = EmailMessage(
            message_id=f"<imap-fetch-{key_digest}@local>",
            uid=uid,
            mailbox=mailbox,
            folder=folder,
            raw_object_key=f"emails/pending/{key_digest}.eml",
            status="failed",
            attempts=attempt_no,
        )
        db.add(row)
    else:
        row.attempts = attempt_no
    row.status = "failed_final" if attempt_no >= MAX_ATTEMPTS else "failed"
    _audit(
        db,
        "email_failed",
        "Email could not be fetched",
        result="error",
        entity_id=row.id,
        detail=_email_exception_detail(uid, exc),
    )
    db.commit()
    counts["failed"] += 1
    return attempt_no >= MAX_ATTEMPTS
