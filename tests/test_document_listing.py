import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db

client = TestClient(app)


def _override(owner_id, db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner_id)}


def _clear_overrides():
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_list_documents_returns_only_authenticated_owner_page():
    owner_id = uuid.uuid4()
    document = SimpleNamespace(
        id=uuid.uuid4(),
        original_filename="invoice.pdf",
        status="extracted",
        doc_type="invoice",
        failure_message=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    count_query = MagicMock()
    count_query.filter.return_value.scalar.return_value = 1
    page_query = MagicMock()
    page_results = (
        page_query.filter.return_value.order_by.return_value.offset.return_value
        .limit.return_value.all
    )
    page_results.return_value = [document]
    db = MagicMock()
    db.query.side_effect = [count_query, page_query]
    _override(owner_id, db)

    try:
        response = client.get(
            "/api/documents?page=1&page_size=10&status=extracted"
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(document.id)
    assert body["items"][0]["status"] == "extracted"
    assert len(page_query.filter.call_args.args) == 2
    owner_filter = page_query.filter.call_args.args[0]
    compiled = owner_filter.compile(dialect=postgresql.dialect())
    assert "documents.created_by" in str(compiled)
    assert "invoices.owner_user_id" in str(compiled)


def test_empty_document_workspace_returns_zero_from_owner_scoped_api():
    owner_id = uuid.uuid4()
    count_query = MagicMock()
    count_query.filter.return_value.scalar.return_value = 0
    page_query = MagicMock()
    (
        page_query.filter.return_value.order_by.return_value.offset.return_value
        .limit.return_value.all.return_value
    ) = []
    db = MagicMock()
    db.query.side_effect = [count_query, page_query]
    _override(owner_id, db)

    try:
        response = client.get("/api/documents")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0


def test_document_detail_is_not_found_for_another_owner():
    owner_id = uuid.uuid4()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    _override(owner_id, db)

    try:
        response = client.get(f"/api/documents/{uuid.uuid4()}")
    finally:
        _clear_overrides()

    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found"


def test_document_detail_includes_evidence_and_audit_history():
    owner_id = uuid.uuid4()
    document_id = uuid.uuid4()
    document = SimpleNamespace(
        id=document_id,
        original_filename="invoice.pdf",
        status="needs_vendor_review",
        doc_type="invoice",
        failure_message="Vendor requires review",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        raw_text="Invoice source text",
        extracted_json={"invoice_number": "INV-1"},
    )
    event = SimpleNamespace(
        action="document_processing_retry_requested",
        result="success",
        message="Retry requested",
        old_value={"status": "failed"},
        new_value={"status": "received"},
        created_by=owner_id,
        created_at=datetime.now(UTC),
    )
    document_query = MagicMock()
    document_query.filter.return_value.first.return_value = document
    audit_query = MagicMock()
    audit_results = (
        audit_query.filter.return_value.order_by.return_value.limit.return_value.all
    )
    audit_results.return_value = [event]
    db = MagicMock()
    db.query.side_effect = [document_query, audit_query]
    _override(owner_id, db)

    try:
        response = client.get(f"/api/documents/{document_id}")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["raw_text"] == "Invoice source text"
    assert body["extracted_json"] == {"invoice_number": "INV-1"}
    assert body["audit_history"][0]["action"] == (
        "document_processing_retry_requested"
    )
