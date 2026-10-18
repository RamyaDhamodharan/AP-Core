import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db

client = TestClient(app)


def _setup_dashboard(rows=None, total=0):
    count_query = MagicMock()
    count_query.select_from.return_value = count_query
    count_query.outerjoin.return_value = count_query
    count_query.filter.return_value = count_query
    count_query.scalar.return_value = total

    invoices_query = MagicMock()
    invoices_query.outerjoin.return_value = invoices_query
    invoices_query.filter.return_value = invoices_query
    invoices_query.order_by.return_value = invoices_query
    invoices_query.offset.return_value = invoices_query
    invoices_query.limit.return_value = invoices_query
    invoices_query.all.return_value = rows or []

    db = MagicMock()
    db.query.side_effect = [count_query, invoices_query]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(uuid.uuid4()),
        "type": "access",
    }

    def teardown():
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    return db, count_query, invoices_query, teardown


def _invoice(**overrides):
    values = {
        "id": uuid.uuid4(),
        "invoice_no": "INV-101",
        "vendor_name_raw": "ABC Supplies Ltd",
        "invoice_date": date(2026, 10, 1),
        "due_date": date(2026, 10, 31),
        "total_amount": Decimal("125.50"),
        "currency": "USD",
        "match_status": "ready_for_review",
        "final_status": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_invoice_list_is_paginated_and_returns_dashboard_fields():
    invoice = _invoice()
    db, _, query, teardown = _setup_dashboard(
        [(invoice, "ABC Supplies")],
        total=41,
    )

    try:
        response = client.get(
            "/api/invoices",
            params={"page": 3, "page_size": 10},
        )
    finally:
        teardown()

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 41
    assert body["page"] == 3
    assert body["page_size"] == 10
    assert body["items"] == [
        {
            "invoice_id": str(invoice.id),
            "invoice_no": "INV-101",
            "vendor_name": "ABC Supplies",
            "invoice_date": "2026-10-01",
            "due_date": "2026-10-31",
            "total_amount": "125.50",
            "currency": "USD",
            "match_status": "ready_for_review",
            "final_status": None,
        }
    ]
    query.offset.assert_called_once_with(20)
    query.limit.assert_called_once_with(10)
    assert db.query.call_count == 2


def test_invoice_list_applies_filters_and_search():
    vendor_id = uuid.uuid4()
    _, _, query, teardown = _setup_dashboard()

    try:
        response = client.get(
            "/api/invoices",
            params={
                "match_status": "match_issues",
                "final_status": "on_hold",
                "vendor_id": str(vendor_id),
                "invoice_date_from": "2026-09-01",
                "invoice_date_to": "2026-10-01",
                "due_date_from": "2026-10-15",
                "due_date_to": "2026-11-01",
                "search": "INV-10",
            },
        )
    finally:
        teardown()

    assert response.status_code == 200
    expressions = query.filter.call_args.args
    compiled = [
        expression.compile(dialect=postgresql.dialect())
        for expression in expressions
    ]
    sql = " ".join(str(statement) for statement in compiled)
    params = {
        value
        for statement in compiled
        for value in statement.params.values()
    }
    assert "invoices.match_status" in sql
    assert "invoices.final_status" in sql
    assert "invoices.vendor_id" in sql
    assert "invoices.invoice_date" in sql
    assert "invoices.due_date" in sql
    assert "invoices.invoice_no" in sql
    assert "invoices.vendor_name_raw" in sql
    assert "vendors.name" in sql
    assert "%INV-10%" in params


def test_review_queue_includes_only_undecided_invoices():
    _, _, query, teardown = _setup_dashboard()

    try:
        response = client.get("/api/invoices/review-queue")
    finally:
        teardown()

    assert response.status_code == 200
    expressions = query.filter.call_args.args
    compiled_sql = [
        str(expression.compile(dialect=postgresql.dialect()))
        for expression in expressions
    ]
    assert any(
        "invoices.final_status IS NULL" in statement
        for statement in compiled_sql
    )


def test_invoice_list_returns_empty_page():
    _, _, query, teardown = _setup_dashboard(rows=[], total=0)

    try:
        response = client.get("/api/invoices")
    finally:
        teardown()

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0
    query.all.assert_called_once()


def test_invoice_list_rejects_invalid_pagination():
    db, _, _, teardown = _setup_dashboard()

    try:
        response = client.get("/api/invoices?page=0&page_size=101")
    finally:
        teardown()

    assert response.status_code == 422
    db.query.assert_not_called()


def test_invoice_list_rejects_invalid_filters_and_date_ranges():
    invalid_requests = (
        "/api/invoices?match_status=unknown",
        "/api/invoices?final_status=pending",
        "/api/invoices?vendor_id=not-a-uuid",
        "/api/invoices?invoice_date_from=2026-10-10&invoice_date_to=2026-10-01",
        "/api/invoices?due_date=2026-10-01&due_date_from=2026-09-01",
        "/api/invoices?search=%20%20",
    )

    for url in invalid_requests:
        db, _, _, teardown = _setup_dashboard()
        try:
            response = client.get(url)
        finally:
            teardown()
        assert response.status_code == 422, url
        db.query.assert_not_called()


def test_invoice_list_requires_authentication():
    db, _, _, teardown = _setup_dashboard()
    app.dependency_overrides.pop(get_current_user, None)

    try:
        response = client.get("/api/invoices")
    finally:
        teardown()

    assert response.status_code == 401
    db.query.assert_not_called()
