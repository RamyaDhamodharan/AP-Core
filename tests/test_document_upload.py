
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.routers import document_router
from src.db.session import get_db

client = TestClient(app)


def setup_db(monkeypatch, db):
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(
        document_router,
        "upload_file",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        document_router,
        "delete_file",
        lambda *args, **kwargs: None,
    )


def teardown_db():
    app.dependency_overrides.pop(get_db, None)


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
    db.query.return_value.filter_by.return_value.first.return_value = (
        existing_document
    )

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
