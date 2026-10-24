import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers.invoice_routers import _invoice_dashboard_filters
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.invoice_models import Invoice
from src.services.document_workflow_services import compare_available_records
from src.services.invoice_confidence_services import (
    confidence_level,
    confidence_threshold,
    normalize_invoice_confidence,
    overall_confidence_percent,
    requires_verification,
)


def test_overall_confidence_percent_uses_header_min_and_line_average():
    fields = (
        "vendor",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "subtotal",
        "tax",
        "total",
        "order_number",
        "gl_code",
    )
    confidence = normalize_invoice_confidence(
        {
            **dict.fromkeys(fields, "value"),
            "field_confidence": {
                **{field: {"confidence": 90} for field in fields},
                "total": {"confidence": 86},
            },
            "line_items": [
                {
                    "qty": 1,
                    "unit_price": 2,
                    "line_amount": 2,
                    "field_confidence": {
                        "qty": {"confidence": 80},
                        "unit_price": {"confidence": 100},
                        "line_amount": {"confidence": 100},
                    },
                }
            ],
        }
    )
    assert overall_confidence_percent(confidence) == 86


def test_overall_confidence_percent_is_unscored_when_confidence_is_missing():
    confidence = normalize_invoice_confidence({"vendor": "Only field"})
    assert overall_confidence_percent(confidence) is None
    assert confidence_level(confidence) == "unknown"


client = TestClient(app)


def test_missing_model_confidence_is_derived_and_gates_verification(monkeypatch):
    monkeypatch.setenv("FIELD_CONFIDENCE_MIN", "85")
    confidence = normalize_invoice_confidence(
        {"vendor": "Test Vendor", "invoice_number": "INV-1", "total": 12}
    )
    assert confidence_threshold() == 85
    assert confidence["vendor"] == {"confidence": 0.0, "derived": True}
    assert confidence["due_date"] == {"confidence": 0.0, "derived": True}
    assert requires_verification(confidence)
    assert confidence_level(confidence) == "unknown"


def test_confidence_threshold_allows_verified_model_scores(monkeypatch):
    monkeypatch.setenv("FIELD_CONFIDENCE_MIN", "80")
    fields = (
        "vendor",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "subtotal",
        "tax",
        "total",
        "order_number",
        "gl_code",
    )
    data = dict.fromkeys(fields, "value")
    data["field_confidence"] = {
        field: {"confidence": 95, "derived": False} for field in data
    }
    assert not requires_verification(normalize_invoice_confidence(data))
    assert confidence_level(normalize_invoice_confidence(data)) == "high"


def test_verify_edits_audit_old_new_reason_and_associate_can_continue(monkeypatch):
    owner_id = uuid.uuid4()
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=None,
        workflow_status="verifying",
        match_status="needs_vendor_review",
        final_status=None,
        vendor_name_raw="Old Name",
        invoice_no="INV-1",
        invoice_date=None,
        due_date=None,
        currency="USD",
        subtotal=Decimal("10"),
        tax_amount=Decimal("1"),
        total_amount=Decimal("11"),
        po_number_raw="PO-1",
        po_id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        gl_code="6000",
        field_confidence=normalize_invoice_confidence({"total": 11}),
    )
    invoice_query = MagicMock()
    invoice_query.outerjoin.return_value = invoice_query
    invoice_query.filter.return_value = invoice_query
    invoice_query.with_for_update.return_value = invoice_query
    invoice_query.first.return_value = invoice
    line = SimpleNamespace(
        id=uuid.uuid4(),
        qty=Decimal("1"),
        unit_price=Decimal("10"),
        line_amount=Decimal("10"),
        field_confidence={
            "qty": {"confidence": 70, "derived": True},
            "unit_price": {"confidence": 70, "derived": True},
            "line_amount": {"confidence": 70, "derived": True},
        },
    )
    line_query = MagicMock()
    line_query.filter.return_value.all.return_value = [line]
    db = MagicMock()
    db.query.side_effect = [invoice_query, line_query]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(owner_id),
        "role": "associate",
    }
    monkeypatch.setattr(
        "src.api.rest.routers.invoice_routers.run_match_for_invoice",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.api.rest.routers.invoice_routers.find_duplicate_invoice",
        lambda *_args, **_kwargs: None,
    )
    try:
        response = client.post(
            f"/api/invoices/{invoice.id}/verify",
            json={
                "values": {
                    "gl_code": "6100",
                    "line_items": [{"id": str(line.id), "unit_price": "11"}],
                },
                "reason": "Corrected cost center",
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 200
    assert invoice.gl_code == "6100"
    assert line.unit_price == Decimal("11")
    assert invoice.workflow_status == "matching"
    audit = next(
        obj
        for call in db.add.call_args_list
        for obj in call.args
        if isinstance(obj, AuditLog) and obj.action == "invoice_field_edited"
    )
    assert audit.created_by == owner_id
    assert audit.old_value == {"gl_code": "6000"}
    assert audit.new_value == {"gl_code": "6100", "reason": "Corrected cost center"}
    line_audit = next(
        obj
        for call in db.add.call_args_list
        for obj in call.args
        if isinstance(obj, AuditLog) and obj.action == "invoice_line_field_edited"
    )
    assert line_audit.created_by == owner_id
    assert line_audit.new_value["unit_price"] == "11"


def test_verify_rejects_real_duplicate_invoice_number_with_409(monkeypatch):

    owner_id = uuid.uuid4()
    vendor_id = uuid.uuid4()
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=None,
        workflow_status="verifying",
        match_status="needs_vendor_review",
        invoice_no="INV-1",
        vendor_id=vendor_id,
        vendor_name_raw="Vendor",
    )
    duplicate = Invoice(
        id=uuid.uuid4(),
        invoice_no="INV/2",
        invoice_no_normalized="INV2",
        vendor_id=vendor_id,
        is_current=True,
    )
    invoice_query = MagicMock()
    (
        invoice_query.outerjoin.return_value.filter.return_value.with_for_update.return_value.first.return_value
    ) = invoice
    duplicate_query = MagicMock()
    (
        duplicate_query.filter.return_value.with_for_update.return_value.first.return_value
    ) = duplicate
    db = MagicMock()
    db.query.side_effect = [invoice_query, duplicate_query]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner_id)}
    monkeypatch.setattr(
        "src.api.rest.routers.invoice_routers.run_match_for_invoice",
        lambda *_args: None,
    )
    try:
        response = client.post(
            f"/api/invoices/{invoice.id}/verify",
            json={"values": {"invoice_number": "INV-2"}, "reason": "Confirm number"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "An invoice with this vendor and invoice number already exists"
    )


def test_get_verification_returns_values_confidence_and_gl_warning(monkeypatch):
    owner_id = uuid.uuid4()
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_name_raw="Test Vendor",
        invoice_no="INV-2",
        invoice_date=None,
        due_date=None,
        currency="USD",
        subtotal=Decimal("10"),
        tax_amount=Decimal("1"),
        total_amount=Decimal("11"),
        po_number_raw="PO-2",
        gl_code="9999",
        field_confidence={"total": {"confidence": 40, "derived": True}},
    )
    invoice_query = MagicMock()
    invoice_query.outerjoin.return_value = invoice_query
    invoice_query.filter.return_value.first.return_value = invoice
    line_query = MagicMock()
    line_query.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [invoice_query, line_query]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner_id)}
    monkeypatch.setenv("GL_CODE_LIST", "6100,6200")
    try:
        response = client.get(f"/api/invoices/{invoice.id}/verification")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 200
    result = response.json()
    assert result["fields"]["invoice_number"]["value"] == "INV-2"
    assert result["fields"]["total"]["confidence"] == 40
    assert result["needs_verification"] is True
    assert result["confidence_level"] == "unknown"
    assert result["gl_code_warning"] == "Unknown GL code: 9999"


def test_unmatched_invoice_remains_in_automatic_match_review(monkeypatch):
    owner_id = uuid.uuid4()
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        source_document_id=None,
        workflow_status="verifying",
        match_status="needs_vendor_review",
        status="needs_review",
        po_decision=None,
        final_status=None,
        final_by=None,
        final_at=None,
        vendor_name_raw="Unknown Vendor",
        vendor_tax_id_raw=None,
        vendor_id=None,
        invoice_no="INV-UNKNOWN",
        invoice_date=None,
        due_date=None,
        currency="USD",
        subtotal=Decimal("10"),
        tax_amount=Decimal("0"),
        total_amount=Decimal("10"),
        po_number_raw=None,
        invoice_no_normalized="INVUNKNOWN",
        gl_code=None,
        field_confidence={},
    )
    invoice_query = MagicMock()
    locked_query = (
        invoice_query.outerjoin.return_value.filter.return_value.with_for_update.return_value
    )
    locked_query.first.return_value = invoice
    line_query = MagicMock()
    line_query.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [invoice_query, line_query]
    run = SimpleNamespace(id=uuid.uuid4())

    def match_unlinked(_db, _invoice_id, _actor_id, **_kwargs):
        invoice.workflow_status = "match_issues"
        invoice.match_status = "needs_po_confirmation"
        invoice.po_decision = "awaiting_confirmation"
        invoice.status = "needs_review"
        return run

    monkeypatch.setattr(
        "src.api.rest.routers.invoice_routers.find_duplicate_invoice",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.api.rest.routers.invoice_routers.run_match_for_invoice", match_unlinked
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner_id)}
    try:
        response = client.post(f"/api/invoices/{invoice.id}/verify", json={})
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 200
    assert response.json()["status"] == "needs_review"
    assert response.json()["po_decision"] == "awaiting_confirmation"
    assert invoice.final_status is None
    assert invoice.workflow_status == "match_issues"
    assert db.commit.call_count == 1


def test_verification_edit_requires_reason():
    owner_id = uuid.uuid4()
    invoice = SimpleNamespace(id=uuid.uuid4(), workflow_status="verifying")
    query = MagicMock()
    query.outerjoin.return_value = query
    query.filter.return_value = query
    query.with_for_update.return_value.first.return_value = invoice
    db = MagicMock()
    db.query.return_value = query
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner_id)}
    try:
        response = client.post(
            f"/api/invoices/{invoice.id}/verify",
            json={"values": {"gl_code": "7000"}},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 422
    assert response.json()["detail"] == "A reason is required for edits"


def test_tax_difference_over_tolerance_is_a_match_issue(monkeypatch):
    monkeypatch.setenv("TAX_TOLERANCE_ABS", "0.01")
    monkeypatch.setenv("TAX_TOLERANCE_PERCENT", "0.5")
    invoice = SimpleNamespace(
        vendor_id=uuid.uuid4(),
        po_number_raw="PO-1",
        currency="USD",
        tax_amount=Decimal("11.02"),
        total_amount=Decimal("111.02"),
    )
    po = SimpleNamespace(
        vendor_id=invoice.vendor_id,
        po_number="PO-1",
        currency="USD",
        tax_amount=Decimal("10"),
        total_amount=Decimal("110"),
        subtotal=Decimal("100"),
        tax_rate=None,
    )
    checks = compare_available_records(
        db=None,
        invoice=invoice,
        purchase_order=po,
        receipts=[],
        po_lines=[],
        invoice_lines=[],
    )
    tax_check = next(item for item in checks if item.name == "tax")
    assert tax_check.outcome == "mismatch"
    assert tax_check.difference == Decimal("1.02")


def _tax_check(monkeypatch, invoice_tax, expected_tax, *, subtotal="100", rate=None):
    monkeypatch.setenv("TAX_TOLERANCE_ABS", "0.01")
    monkeypatch.setenv("TAX_TOLERANCE_PERCENT", "0.5")
    invoice = SimpleNamespace(
        vendor_id=uuid.uuid4(),
        po_number_raw="PO-1",
        currency="USD",
        tax_amount=invoice_tax,
        subtotal=Decimal(subtotal),
        total_amount=Decimal("110"),
    )
    po = SimpleNamespace(
        vendor_id=invoice.vendor_id,
        po_number="PO-1",
        currency="USD",
        tax_amount=expected_tax,
        subtotal=Decimal(subtotal),
        tax_rate=rate,
        total_amount=Decimal("110"),
    )
    return next(
        check
        for check in compare_available_records(
            db=None,
            invoice=invoice,
            purchase_order=po,
            receipts=[],
            po_lines=[],
            invoice_lines=[],
        )
        if check.name == "tax"
    )


def test_tax_undetermined_is_a_warning_issue(monkeypatch):
    check = _tax_check(monkeypatch, None, None)
    assert check.outcome == "not_possible"
    assert check.reason == "Tax could not be verified"
    assert check.severity == "warning"


def test_tax_tolerance_uses_absolute_floor_percent_and_currency_rounding(monkeypatch):
    assert (
        _tax_check(monkeypatch, Decimal("10.01"), Decimal("10.00")).outcome == "passed"
    )
    assert (
        _tax_check(
            monkeypatch, Decimal("100.40"), Decimal("100.00"), subtotal="20000"
        ).outcome
        == "passed"
    )
    outside = _tax_check(
        monkeypatch, Decimal("100.51"), Decimal("100.00"), subtotal="20000"
    )
    assert outside.outcome == "mismatch"
    assert outside.reason == "Tax does not match expected"
    rounded = _tax_check(monkeypatch, Decimal("1.004"), Decimal("1.00"))
    assert rounded.outcome == "passed"


def test_invoice_filter_builder_supports_workflow_bucket_amount_gl_and_confidence():
    filters = _invoice_dashboard_filters(
        match_status=None,
        final_status=None,
        vendor_id=None,
        invoice_date_from=None,
        invoice_date_to=None,
        due_date=None,
        due_date_from=None,
        due_date_to=None,
        search=None,
        review_queue=False,
        owner_id=uuid.uuid4(),
        workflow_status="verifying",
        bucket="invoice",
        amount_min=Decimal("10"),
        amount_max=Decimal("100"),
        gl_code="6100",
        confidence_min=85,
    )
    assert len(filters) == 7


def test_verification_migration_and_bootstrap_include_new_fields():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    bootstrap = (root / "database" / "001_initial_schema.sql").read_text()
    migration = (
        root
        / "alembic"
        / "versions"
        / "20261022_0013_invoice_verification_confidence.py"
    ).read_text()
    assert "field_confidence jsonb" in bootstrap
    assert "gl_code varchar(60)" in bootstrap
    assert "tax_rate numeric(7,4)" in bootstrap
    assert "default_tax_rate numeric(7,4)" in bootstrap
    assert "ADD COLUMN IF NOT EXISTS" in migration
    assert "def downgrade()" in migration


def test_goods_receipt_api_is_enabled_by_default():
    assert any(
        path.startswith("/api/goods-receipts") for path in app.openapi()["paths"]
    )
