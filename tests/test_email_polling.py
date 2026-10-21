import imaplib
import uuid
from email.message import EmailMessage
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.routers import email_polling_routers
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.notification_models import Notification
from src.services import document_ingest_services, email_polling_services


class FakeQuery:
    def __init__(self, db, model):
        self.db, self.model, self.filters = db, model, {}

    def filter_by(self, **kwargs):
        self.filters.update(kwargs)
        return self

    def filter(self, *_):
        return self

    def order_by(self, *_):
        return self

    def count(self):
        return 0

    def first(self):
        return next(
            (
                row
                for row in self.db.rows
                if isinstance(row, self.model)
                and all(
                    getattr(row, key, None) == value
                    for key, value in self.filters.items()
                )
            ),
            None,
        )


class FakeDB:
    def __init__(self):
        self.rows = []

    def query(self, model):
        return FakeQuery(self, model)

    def add(self, row):
        if isinstance(row, Document) and row.id is None:
            row.id = uuid.uuid4()
        self.rows.append(row)

    def commit(self):
        pass

    def rollback(self):
        pass

    def refresh(self, _row):
        pass

    def get(self, model, row_id):
        return next(
            (row for row in self.rows if isinstance(row, model) and row.id == row_id),
            None,
        )


class FakeIMAP:
    def __init__(self, messages):
        self.messages = messages
        self.seen = []
        self.logged_out = False

    def login(self, *_):
        return "OK", []

    def select(self, *_):
        return "OK", [b"1"]

    def search(self, *_):
        return "OK", [b" ".join(str(i).encode() for i in self.messages)]

    def fetch(self, number, *_):
        return "OK", [(b"RFC822", self.messages[int(number)])]

    def store(self, number, *_):
        self.seen.append(int(number))
        return "OK", []

    def logout(self):
        self.logged_out = True


def _mail(message_id="<one@vendor.test>", attachments=()):
    message = EmailMessage()
    message["From"] = "billing@test-mail.test"
    message["To"] = "ap@test-mail.test"
    message["Subject"] = "Invoice"
    message["Message-ID"] = message_id
    message["Date"] = "Tue, 21 Oct 2025 10:00:00 +0000"
    message.set_content("Please see attached.")
    for filename, data, maintype, subtype in attachments:
        message.add_attachment(
            data, maintype=maintype, subtype=subtype, filename=filename
        )
    return message.as_bytes()


@pytest.fixture
def polling_setup(monkeypatch):
    db = FakeDB()
    monkeypatch.setenv("IMAP_HOST", "imap.test")
    monkeypatch.setenv("IMAP_USER", "user")
    monkeypatch.setenv("IMAP_PASSWORD", "pass")
    monkeypatch.setenv("USE_CELERY", "true")
    monkeypatch.setattr(document_ingest_services, "upload_file", lambda *_: None)
    monkeypatch.setattr(document_ingest_services, "delete_file", lambda *_: None)
    monkeypatch.setattr(
        document_ingest_services, "dispatch_document_processing", lambda *_: "celery"
    )
    monkeypatch.setattr(
        email_polling_services, "dispatch_queued_notifications", lambda *_: 1
    )
    return db


def _poll(db, monkeypatch, messages):
    client = FakeIMAP(messages)
    result = email_polling_services.poll_email_messages(
        db, lambda *_: client, sleep=lambda _: None
    )
    return result, client


def test_pdf_email_creates_parent_child_dispatches_and_marks_seen(
    polling_setup, monkeypatch
):
    db = polling_setup
    dispatches = []
    monkeypatch.setattr(
        document_ingest_services,
        "dispatch_document_processing",
        lambda *args: dispatches.append(args[1]),
    )
    result, client = _poll(
        db,
        monkeypatch,
        {1: _mail(attachments=[("bill.pdf", b"pdf", "application", "pdf")])},
    )
    docs = [row for row in db.rows if isinstance(row, Document)]
    assert result["attachments"] == 1
    assert len(docs) == 2
    assert docs[0].email_message_id == "<one@vendor.test>"
    assert docs[1].parent_document_id == docs[0].id
    assert docs[1].id in dispatches
    assert client.seen == [1]


def test_duplicate_message_id_is_skipped(polling_setup, monkeypatch):
    db = polling_setup
    mail = _mail(attachments=[("bill.pdf", b"pdf", "application", "pdf")])
    first, _ = _poll(db, monkeypatch, {1: mail})
    assert (
        next(row for row in db.rows if isinstance(row, Document)).email_message_id
        == "<one@vendor.test>"
    )
    second, client = _poll(db, monkeypatch, {2: mail})
    assert first["attachments"] == 1
    assert second["duplicates"] == 1
    assert len([row for row in db.rows if isinstance(row, Document)]) == 2
    assert client.seen == [2]
    assert any(
        isinstance(row, AuditLog) and row.action == "email_skipped_duplicate"
        for row in db.rows
    )


def test_no_attachment_marks_parent_ignored_and_queues_notification(
    polling_setup, monkeypatch
):
    db = polling_setup
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "billing@test-mail.test")
    _poll(db, monkeypatch, {1: _mail()})
    parent = next(row for row in db.rows if isinstance(row, Document))
    assert parent.status == "ignored"
    assert parent.failure_message == "No bill attachment found"
    assert any(
        isinstance(row, Notification) and row.status == "queued" for row in db.rows
    )


def test_bad_attachment_does_not_block_good_attachment(polling_setup, monkeypatch):
    db = polling_setup

    def fail_bad(_key, content, _type):
        if content == b"bad":
            raise ValueError("corrupt attachment")

    monkeypatch.setattr(document_ingest_services, "upload_file", fail_bad)
    raw = _mail(
        attachments=[
            ("bad.pdf", b"bad", "application", "pdf"),
            ("good.pdf", b"good", "application", "pdf"),
        ]
    )
    counts, _ = _poll(db, monkeypatch, {1: raw})
    docs = [row for row in db.rows if isinstance(row, Document)]
    assert counts["attachments"] == 1
    assert counts["failed"] == 1
    assert len(docs) == 2
    assert any(
        isinstance(row, AuditLog) and row.action == "email_failed" for row in db.rows
    )


def test_imap_login_failure_is_audited_without_raising(polling_setup, monkeypatch):
    db = polling_setup

    class BrokenIMAP:
        def __init__(self, *_):
            pass

        def login(self, *_):
            raise imaplib.IMAP4.error("bad credentials")

    counts = email_polling_services.poll_email_messages(
        db, BrokenIMAP, sleep=lambda _: None
    )
    assert counts["failed"] == 1
    assert any(
        isinstance(row, AuditLog) and row.action == "email_failed" for row in db.rows
    )


def test_manual_poll_endpoint_requires_admin(monkeypatch):
    secret = "email-poll-test-secret-with-32-bytes"
    monkeypatch.setenv("JWT_SECRET_KEY", secret)
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    monkeypatch.setattr(
        email_polling_routers,
        "poll_email_with_lock",
        lambda _db: {"emails": 0, "attachments": 0, "duplicates": 0, "failed": 0},
    )
    try:
        associate = jwt.encode(
            {"sub": str(uuid.uuid4()), "type": "access", "role": "associate"},
            secret,
            algorithm="HS256",
        )
        admin = jwt.encode(
            {"sub": str(uuid.uuid4()), "type": "access", "role": "admin"},
            secret,
            algorithm="HS256",
        )
        assert (
            client.post(
                "/api/email-polling/run",
                headers={"Authorization": f"Bearer {associate}"},
            ).status_code
            == 403
        )
        response = client.post(
            "/api/email-polling/run", headers={"Authorization": f"Bearer {admin}"}
        )
        assert response.status_code == 200
        assert response.json()["emails"] == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
