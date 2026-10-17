
import sys
import uuid
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.routers import document_router

client = TestClient(app)


def test_parse_document_success(monkeypatch):
    document_id = uuid.uuid4()

    document = SimpleNamespace(
        id=document_id,
        original_filename="invoice.pdf",
        storage_path=f"documents/{document_id}.pdf",
        status="received",
        doc_type=None,
        raw_text=None,
        extracted_json=None,
        failure_message=None,
    )

    # Mock database session
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = document

    # Mock file download from MinIO
    monkeypatch.setattr(
        document_router,
        "download_file",
        lambda path: b"fake PDF content",
    )

    # Mock Docling document conversion
    class FakeConverter:
        def convert(self, path):
            return SimpleNamespace(
                document=SimpleNamespace(
                    export_to_markdown=lambda: (
                        "Supplier invoice INV-101. Total: 1500"
                    )
                )
            )

    # Register fake Docling modules so Docling need not be installed
    fake_docling = ModuleType("docling")
    fake_converter_module = ModuleType("docling.document_converter")
    fake_converter_module.DocumentConverter = FakeConverter

    monkeypatch.setitem(sys.modules, "docling", fake_docling)
    monkeypatch.setitem(
        sys.modules,
        "docling.document_converter",
        fake_converter_module,
    )

    # Mock classification and extraction; no real LLM API calls
    monkeypatch.setattr(
        document_router,
        "classify_document",
        lambda text: "invoice",
    )

    monkeypatch.setattr(
        document_router,
        "extract_document",
        lambda text, doc_type: {
            "invoice_number": "INV-101",
            "total": 1500,
        },
    )

    # Override FastAPI database dependency
    from src.db.session import get_db

    app.dependency_overrides[get_db] = lambda: db

    try:
        response = client.post(
            f"/api/documents/{document_id}/parse"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    # Verify API response
    assert response.status_code == 200
    assert response.json()["status"] == "extracted"
    assert response.json()["doc_type"] == "invoice"
    assert response.json()["extracted_json"]["invoice_number"] == "INV-101"

    # Verify pipeline data and final document status
    assert document.raw_text == "Supplier invoice INV-101. Total: 1500"
    assert document.status == "extracted"
    assert document.doc_type == "invoice"
    assert document.extracted_json == {
        "invoice_number": "INV-101",
        "total": 1500,
    }

def test_parse_document_not_found(monkeypatch):
    from src.db.session import get_db

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None

    app.dependency_overrides[get_db] = lambda: db

    try:
        response = client.post(
            f"/api/documents/{uuid.uuid4()}/parse"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 404


def test_parse_document_download_failure(monkeypatch):
    from src.db.session import get_db

    document_id = uuid.uuid4()

    document = SimpleNamespace(
        id=document_id,
        original_filename="invoice.pdf",
        storage_path=f"documents/{document_id}.pdf",
        status="received",
        doc_type=None,
        raw_text=None,
        extracted_json=None,
        failure_message=None,
    )

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = document

    def fail_download(path):
        raise RuntimeError("MinIO download failed")

    monkeypatch.setattr(
        document_router,
        "download_file",
        fail_download,
    )

    app.dependency_overrides[get_db] = lambda: db

    try:
        response = client.post(
            f"/api/documents/{document_id}/parse"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code >= 400


def test_parse_document_classification_failure(monkeypatch):
    from src.db.session import get_db

    document_id = uuid.uuid4()

    document = SimpleNamespace(
        id=document_id,
        original_filename="invoice.pdf",
        storage_path=f"documents/{document_id}.pdf",
        status="received",
        doc_type=None,
        raw_text=None,
        extracted_json=None,
        failure_message=None,
    )

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = document

    monkeypatch.setattr(
        document_router,
        "download_file",
        lambda path: b"fake PDF content",
    )

    class FakeConverter:
        def convert(self, path):
            return SimpleNamespace(
                document=SimpleNamespace(
                    export_to_markdown=lambda: "Invoice INV-101. Total 1500"
                )
            )

    fake_docling = ModuleType("docling")
    fake_converter_module = ModuleType("docling.document_converter")
    fake_converter_module.DocumentConverter = FakeConverter

    monkeypatch.setitem(sys.modules, "docling", fake_docling)
    monkeypatch.setitem(
        sys.modules,
        "docling.document_converter",
        fake_converter_module,
    )

    def fail_classification(text):
        raise RuntimeError("Classification failed")

    monkeypatch.setattr(
        document_router,
        "classify_document",
        fail_classification,
    )

    app.dependency_overrides[get_db] = lambda: db

    try:
        response = client.post(
            f"/api/documents/{document_id}/parse"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code >= 400


def test_parse_document_extraction_failure(monkeypatch):
    from src.db.session import get_db

    document_id = uuid.uuid4()

    document = SimpleNamespace(
        id=document_id,
        original_filename="invoice.pdf",
        storage_path=f"documents/{document_id}.pdf",
        status="received",
        doc_type=None,
        raw_text=None,
        extracted_json=None,
        failure_message=None,
    )

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = document

    monkeypatch.setattr(
        document_router,
        "download_file",
        lambda path: b"fake PDF content",
    )

    class FakeConverter:
        def convert(self, path):
            return SimpleNamespace(
                document=SimpleNamespace(
                    export_to_markdown=lambda: "Invoice INV-101. Total 1500"
                )
            )

    fake_docling = ModuleType("docling")
    fake_converter_module = ModuleType("docling.document_converter")
    fake_converter_module.DocumentConverter = FakeConverter

    monkeypatch.setitem(sys.modules, "docling", fake_docling)
    monkeypatch.setitem(
        sys.modules,
        "docling.document_converter",
        fake_converter_module,
    )

    monkeypatch.setattr(
        document_router,
        "classify_document",
        lambda text: "invoice",
    )

    def fail_extraction(text, doc_type):
        raise RuntimeError("Extraction failed")

    monkeypatch.setattr(
        document_router,
        "extract_document",
        fail_extraction,
    )

    app.dependency_overrides[get_db] = lambda: db

    try:
        response = client.post(
            f"/api/documents/{document_id}/parse"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code >= 400
