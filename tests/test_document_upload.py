import sys
import uuid
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import jwt
from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers import document_routers
from src.db.session_db import get_db

client = TestClient(app)


def setup_db(monkeypatch, db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4())}
    monkeypatch.setattr(
        document_routers,
        "upload_file",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        document_routers,
        "delete_file",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        document_routers,
        "_process_in_background",
        lambda *args, **kwargs: None,
    )


def teardown_db():
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_upload_valid_pdf(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    setup_db(monkeypatch, db)

    try:
        response = client.post(
            "/api/documents/upload",
            files={
                "file": (
                    "invoice.pdf",
                    b"fake PDF content",
                    "application/pdf",
                )
            },
        )
    finally:
        teardown_db()

    assert response.status_code == 201
    body = response.json()
    assert body["original_filename"] == "invoice.pdf"
    assert body["status"] == "received"
    assert "id" in body

    db.add.assert_called_once()
    db.commit.assert_called_once()


def test_upload_extract_pipeline_matches_and_approves_without_manual_route(monkeypatch):
    from src.models.document_models import Document
    from src.models.invoice_models import InvoiceLine
    from src.models.match_run_models import MatchRun
    from src.models.notification_models import Notification
    from src.models.purchase_order_models import PurchaseOrderLine
    from src.services import invoice_matching_services

    owner_id = uuid.uuid4()
    po = SimpleNamespace(id=uuid.uuid4())
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        po_id=po.id,
        source_document_id=None,
        final_status=None,
        final_run_id=None,
        final_by=None,
        final_at=None,
        match_status="pending",
        invoice_no="INV-1",
    )
    db = MagicMock()
    document = None
    document_query = MagicMock()
    document_query.filter_by.return_value.first.return_value = None
    locked_document_query = (
        document_query.filter.return_value.with_for_update.return_value
    )
    locked_document_query.first.side_effect = lambda: document
    empty_query = MagicMock()
    empty_query.filter.return_value.all.return_value = []
    empty_query.all.return_value = []
    previous_query = MagicMock()
    previous_query.filter.return_value.order_by.return_value.first.return_value = None
    max_query = MagicMock()
    max_query.filter.return_value.scalar.return_value = 0
    notification_query = MagicMock()
    notification_query.filter.return_value.all.return_value = []
    match_query_number = 0

    def query(model, *args):
        nonlocal match_query_number
        if model is Document:
            return document_query
        if model in {InvoiceLine, PurchaseOrderLine}:
            return empty_query
        if model is MatchRun:
            match_query_number += 1
            return previous_query
        if str(model).startswith("max("):
            return max_query
        if model is Notification:
            return notification_query
        raise AssertionError(f"Unexpected query model: {model}")

    db.query.side_effect = query
    db.get.side_effect = lambda model, _id: (
        invoice
        if model.__name__ == "Invoice"
        else po
        if model.__name__ == "PurchaseOrder"
        else document
    )

    def add(row):
        nonlocal document
        if isinstance(row, Document):
            document = row

    db.add.side_effect = add

    def flush():
        for call in db.add.call_args_list:
            row = call.args[0]
            if isinstance(row, MatchRun) and row.id is None:
                row.id = uuid.uuid4()

    db.flush.side_effect = flush
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner_id)}
    monkeypatch.setattr(document_routers, "upload_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(document_routers, "download_file", lambda *_: b"PDF")
    monkeypatch.setenv("USE_CELERY", "false")

    class FakeConverter:
        def convert(self, _path):
            return SimpleNamespace(
                document=SimpleNamespace(export_to_markdown=lambda: "Invoice INV-1")
            )

    fake_docling = ModuleType("docling")
    fake_converter = ModuleType("docling.document_converter")
    fake_converter.DocumentConverter = FakeConverter
    monkeypatch.setitem(sys.modules, "docling", fake_docling)
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_converter)
    monkeypatch.setattr(document_routers, "classify_document", lambda _: "invoice")
    monkeypatch.setattr(
        document_routers,
        "extract_document",
        lambda *_: {
            "document_type": "invoice",
            "vendor": "Acme",
            "invoice_number": "INV-1",
            "invoice_date": "2026-10-01",
            "currency": "USD",
            "total": "10",
            "subtotal": "10",
            "tax": "0",
            "line_items": [],
        },
    )

    def save_invoice(_db, saved_doc, _data):
        nonlocal document
        document = saved_doc
        return invoice

    monkeypatch.setattr(document_routers, "_save_invoice_from_extraction", save_invoice)
    monkeypatch.setattr(
        invoice_matching_services,
        "_invoice_state_fingerprint",
        lambda *_: "pipeline-state",
    )
    monkeypatch.setattr(
        invoice_matching_services,
        "recommend_invoice_review",
        lambda *_args, **_kwargs: SimpleNamespace(checks=[]),
    )

    def process_document(document_id):
        document_routers.parse_document(
            document_id=document_id,
            background_tasks=None,
            db=db,
            current_user={"sub": str(owner_id)},
        )

    monkeypatch.setattr(document_routers, "_process_in_background", process_document)
    try:
        response = client.post(
            "/api/documents/upload",
            files={"file": ("invoice.pdf", b"PDF", "application/pdf")},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 201
    assert document.status == "extracted"
    assert invoice.final_status == "approved"
    assert any(
        isinstance(call.args[0], MatchRun) and call.args[0].result == "match_ok"
        for call in db.add.call_args_list
    )


def test_upload_rejects_unsupported_extension(monkeypatch):
    db = MagicMock()
    setup_db(monkeypatch, db)

    try:
        response = client.post(
            "/api/documents/upload",
            files={
                "file": (
                    "malware.exe",
                    b"example content",
                    "application/octet-stream",
                )
            },
        )
    finally:
        teardown_db()

    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported file type"
    db.add.assert_not_called()


def test_upload_rejects_empty_file(monkeypatch):
    db = MagicMock()
    setup_db(monkeypatch, db)

    try:
        response = client.post(
            "/api/documents/upload",
            files={
                "file": (
                    "empty.pdf",
                    b"",
                    "application/pdf",
                )
            },
        )
    finally:
        teardown_db()

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file is empty"
    db.add.assert_not_called()


def test_upload_rejects_file_over_20_mb(monkeypatch):
    db = MagicMock()
    setup_db(monkeypatch, db)

    oversized_content = b"x" * (20 * 1024 * 1024 + 1)

    try:
        response = client.post(
            "/api/documents/upload",
            files={
                "file": (
                    "large.pdf",
                    oversized_content,
                    "application/pdf",
                )
            },
        )
    finally:
        teardown_db()

    assert response.status_code == 413
    assert response.json()["detail"] == "File exceeds 20 MB limit"
    db.add.assert_not_called()


def test_upload_blocks_duplicate_file(monkeypatch):
    existing_id = uuid.uuid4()

    existing_document = SimpleNamespace(
        id=existing_id,
        status="received",
    )

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing_document

    setup_db(monkeypatch, db)

    try:
        response = client.post(
            "/api/documents/upload",
            files={
                "file": (
                    "invoice.pdf",
                    b"same invoice content",
                    "application/pdf",
                )
            },
        )
    finally:
        teardown_db()

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == str(existing_id)
    assert body["status"] == "duplicate_blocked"
    assert body["message"] == "This file has already been uploaded"

    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_upload_triggers_auto_processing(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    setup_db(monkeypatch, db)

    started = []
    monkeypatch.setattr(
        document_routers,
        "_process_in_background",
        lambda document_id: started.append(document_id),
    )
    try:
        response = client.post(
            "/api/documents/upload",
            files={"file": ("invoice.pdf", b"pdf bytes", "application/pdf")},
        )
    finally:
        teardown_db()

    assert response.status_code == 201
    assert started == [uuid.UUID(response.json()["id"])]


def test_uuid_subject_upload_enqueues_document(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    setup_db(monkeypatch, db)
    app.dependency_overrides.pop(get_current_user, None)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-authentication-tests")
    owner_id = uuid.uuid4()
    token = jwt.encode(
        {"sub": str(owner_id), "type": "access"},
        "test-secret-for-authentication-tests",
        algorithm="HS256",
    )
    queued = []
    monkeypatch.setattr(
        document_routers,
        "dispatch_document_processing",
        lambda _tasks, document_id, _fallback: queued.append(document_id),
    )

    try:
        response = client.post(
            "/api/documents/upload",
            files={"file": ("invoice.pdf", b"pdf bytes", "application/pdf")},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        teardown_db()

    assert response.status_code == 201
    assert queued == [uuid.UUID(response.json()["id"])]
    assert db.add.call_args.args[0].created_by == owner_id


def test_background_processing_uses_stored_uploader_identity(monkeypatch):
    owner_id = uuid.uuid4()
    document = SimpleNamespace(id=uuid.uuid4(), status="received", created_by=owner_id)
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = document
    monkeypatch.setattr(document_routers, "SessionLocal", lambda: db)
    processed = []
    monkeypatch.setattr(
        document_routers,
        "parse_document",
        lambda **kwargs: processed.append(kwargs),
    )

    document_routers._process_in_background(document.id)

    assert processed == [
        {
            "document_id": document.id,
            "background_tasks": None,
            "db": db,
            "current_user": {"sub": str(owner_id)},
        }
    ]
    db.close.assert_called_once()


def test_duplicate_upload_does_not_trigger_processing(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = SimpleNamespace(
        id=uuid.uuid4()
    )
    setup_db(monkeypatch, db)

    started = []
    monkeypatch.setattr(
        document_routers,
        "_process_in_background",
        lambda document_id: started.append(document_id),
    )
    try:
        response = client.post(
            "/api/documents/upload",
            files={"file": ("invoice.pdf", b"pdf bytes", "application/pdf")},
        )
    finally:
        teardown_db()

    assert response.json()["status"] == "duplicate_blocked"
    assert started == []
