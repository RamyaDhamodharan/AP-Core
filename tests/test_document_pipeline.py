import sys
import uuid
from datetime import date
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.api.rest.routers import document_routers
from src.models.invoice_models import Invoice

client = TestClient(app)


def _save_invoice_for_matching(
    *,
    vendor,
    purchase_order,
    existing_invoice=None,
    prior_versions=None,
    extracted_data=None,
    alias_vendor=None,
    alias_matches=None,
):
    from src.models.invoice_models import Invoice
    from src.models.purchase_order_models import PurchaseOrder
    from src.models.vendor_models import Vendor, VendorAlias

    document = SimpleNamespace(id=uuid.uuid4())
    data = {
        "vendor": "ABC Supplies",
        "invoice_number": "INV-101",
        "order_number": "PO-101",
        "invoice_date": "2026-10-01",
        "currency": "USD",
        "subtotal": "1500.00",
        "tax": "0",
        "total": "1500.00",
    }
    if extracted_data:
        data.update(extracted_data)

    if existing_invoice is not None and not hasattr(
        existing_invoice,
        "is_current",
    ):
        existing_invoice.is_current = True

    invoice_query = MagicMock()
    source_query_result = invoice_query.filter.return_value
    source_query_result.with_for_update.return_value.first.return_value = (
        existing_invoice
    )
    versions_query = MagicMock()
    versions_query_result = (
        versions_query.filter.return_value.order_by.return_value.with_for_update.return_value
    )
    versions_query_result.all.return_value = prior_versions or []
    db_version_query = versions_query
    invoice_query_count = 0
    vendor_query = MagicMock()
    vendor_query.filter.return_value.all.return_value = (
        [vendor] if vendor is not None else []
    )
    alias_query = MagicMock()
    alias_query.join.return_value.filter.return_value.all.return_value = (
        alias_matches
        if alias_matches is not None
        else ([(SimpleNamespace(), alias_vendor)] if alias_vendor is not None else [])
    )
    po_query = MagicMock()
    po_query.filter.return_value.first.return_value = purchase_order
    db = MagicMock()

    def query_model(*models):
        model = models[0]
        nonlocal invoice_query_count
        if model is Invoice:
            invoice_query_count += 1
            return invoice_query if invoice_query_count == 1 else versions_query
        if model is Vendor:
            return vendor_query
        if model is VendorAlias or models == (VendorAlias, Vendor):
            return alias_query
        if model is PurchaseOrder:
            return po_query
        raise AssertionError(f"Unexpected queried model: {model}")

    db.query.side_effect = query_model
    db.version_query = db_version_query

    invoice = document_routers._save_invoice_from_extraction(
        db,
        document,
        data,
    )
    return invoice, db, document


def _run_purchase_order_parse(
    monkeypatch,
    extracted_data,
    vendor=None,
    duplicate=None,
    alias_vendor=None,
):
    from src.db.session_db import get_db
    from src.models.document_models import Document
    from src.models.purchase_order_models import PurchaseOrder
    from src.models.vendor_models import Vendor, VendorAlias

    document_id = uuid.uuid4()
    document = SimpleNamespace(
        id=document_id,
        original_filename="purchase-order.pdf",
        storage_path=f"documents/{document_id}.pdf",
        status="received",
        doc_type=None,
        raw_text=None,
        extracted_json=None,
        failure_message=None,
        created_by=uuid.uuid4(),
    )

    document_query = MagicMock()
    document_query.filter.return_value.first.return_value = document
    locked_document_query = (
        document_query.filter.return_value.with_for_update.return_value
    )
    locked_document_query.first.return_value = document
    vendor_query = MagicMock()
    vendor_query.filter.return_value.all.return_value = (
        [vendor] if vendor is not None else []
    )
    alias_query = MagicMock()
    alias_query.join.return_value.filter.return_value.all.return_value = (
        [(SimpleNamespace(), alias_vendor)] if alias_vendor is not None else []
    )
    purchase_order_query = MagicMock()
    purchase_order_query.filter.return_value.first.return_value = duplicate
    (
        purchase_order_query.filter.return_value.filter.return_value.first.return_value
    ) = duplicate
    invoice_query = MagicMock()
    invoice_query.filter.return_value.with_for_update.return_value.all.return_value = []

    db = MagicMock()

    def query_model(*models):
        model = models[0]
        return {
            Document: document_query,
            Vendor: vendor_query,
            VendorAlias: alias_query,
            PurchaseOrder: purchase_order_query,
            Invoice: invoice_query,
        }[model]

    db.query.side_effect = query_model

    monkeypatch.setattr(
        document_routers,
        "download_file",
        lambda path: b"fake PDF content",
    )

    class FakeConverter:
        def convert(self, path):
            return SimpleNamespace(
                document=SimpleNamespace(
                    export_to_markdown=lambda: "Purchase order PO-101"
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
        document_routers,
        "classify_document",
        lambda text: "po",
    )
    monkeypatch.setattr(
        document_routers,
        "extract_document",
        lambda text, doc_type: extracted_data,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4())}

    try:
        response = client.post(f"/api/documents/{document_id}/parse")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    return response, document, db


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
    query = db.query.return_value
    query.filter.return_value.first.return_value = document
    locked_query = query.filter.return_value.with_for_update.return_value
    locked_query.first.return_value = document
    saved_invoice = SimpleNamespace(
        id=uuid.uuid4(), po_id=uuid.uuid4(), final_status=None
    )
    monkeypatch.setattr(
        document_routers,
        "_save_invoice_from_extraction",
        lambda *args, **kwargs: saved_invoice,
    )

    def auto_match(_db, invoice_id, user_id):
        assert invoice_id == saved_invoice.id
        saved_invoice.final_status = "approved"

    monkeypatch.setattr(document_routers, "run_match_for_invoice", auto_match)

    # Mock file download from MinIO
    monkeypatch.setattr(
        document_routers,
        "download_file",
        lambda path: b"fake PDF content",
    )

    # Mock Docling document conversion
    class FakeConverter:
        def convert(self, path):
            return SimpleNamespace(
                document=SimpleNamespace(
                    export_to_markdown=lambda: "Supplier invoice INV-101. Total: 1500"
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
        document_routers,
        "classify_document",
        lambda text: "invoice",
    )

    monkeypatch.setattr(
        document_routers,
        "extract_document",
        lambda text, doc_type: {
            "document_type": "invoice",
            "vendor": "ABC Supplies",
            "invoice_number": "INV-101",
            "invoice_date": "2026-10-01",
            "subtotal": 1500,
            "tax": 0,
            "total": 1500,
            "line_items": [],
        },
    )

    # Override FastAPI database dependency
    from src.db.session_db import get_db

    app.dependency_overrides[get_db] = lambda: db
    owner_id = uuid.uuid4()
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-authentication-tests")
    token = jwt.encode(
        {"sub": str(owner_id), "type": "access"},
        "test-secret-for-authentication-tests",
        algorithm="HS256",
    )

    try:
        response = client.post(
            f"/api/documents/{document_id}/parse",
            headers={"Authorization": f"Bearer {token}"},
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
        "document_type": "invoice",
        "vendor": "ABC Supplies",
        "invoice_number": "INV-101",
        "invoice_date": "2026-10-01",
        "subtotal": 1500,
        "tax": 0,
        "total": 1500,
        "line_items": [],
    }
    assert saved_invoice.final_status == "approved"


def test_invoice_matching_sets_po_and_ready_status():
    vendor = SimpleNamespace(id=uuid.uuid4())
    po = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=vendor.id,
        currency="USD",
        total_amount=1500,
    )

    invoice, db, _ = _save_invoice_for_matching(
        vendor=vendor,
        purchase_order=po,
    )

    assert invoice.po_id == po.id
    assert invoice.match_status == "ready_for_review"
    db.add.assert_called_once_with(invoice)


def test_invoice_matching_accepts_normalized_canonical_vendor_name():
    vendor = SimpleNamespace(id=uuid.uuid4())
    po = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=vendor.id,
        currency="USD",
        total_amount=1500,
    )

    invoice, _, _ = _save_invoice_for_matching(
        vendor=vendor,
        purchase_order=po,
        extracted_data={"vendor": "  aBc   sUPPLIES  "},
    )

    assert invoice.vendor_id == vendor.id
    assert invoice.match_status == "ready_for_review"


def test_invoice_matching_resolves_vendor_alias():
    vendor = SimpleNamespace(id=uuid.uuid4())
    po = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=vendor.id,
        currency="USD",
        total_amount=1500,
    )

    invoice, _, _ = _save_invoice_for_matching(
        vendor=None,
        alias_vendor=vendor,
        purchase_order=po,
        extracted_data={"vendor": "  aLtErNaTe   SuPpLiEr "},
    )

    assert invoice.vendor_id == vendor.id
    assert invoice.po_id == po.id
    assert invoice.match_status == "ready_for_review"


def test_invoice_matching_waits_when_po_is_missing():
    vendor = SimpleNamespace(id=uuid.uuid4())

    invoice, _, _ = _save_invoice_for_matching(
        vendor=vendor,
        purchase_order=None,
    )

    assert invoice.po_id is None
    assert invoice.match_status == "waiting_for_po"


def test_invoice_matching_flags_vendor_mismatch():
    vendor = SimpleNamespace(id=uuid.uuid4())
    po = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        currency="USD",
        total_amount=1500,
    )

    invoice, _, _ = _save_invoice_for_matching(
        vendor=vendor,
        purchase_order=po,
    )

    assert invoice.po_id == po.id
    assert invoice.match_status == "match_issues"


def test_invoice_matching_flags_currency_mismatch():
    vendor = SimpleNamespace(id=uuid.uuid4())
    po = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=vendor.id,
        currency="EUR",
        total_amount=1500,
    )

    invoice, _, _ = _save_invoice_for_matching(
        vendor=vendor,
        purchase_order=po,
    )

    assert invoice.po_id == po.id
    assert invoice.match_status == "match_issues"


def test_invoice_matching_flags_amount_mismatch():
    vendor = SimpleNamespace(id=uuid.uuid4())
    po = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=vendor.id,
        currency="USD",
        total_amount=1500,
    )

    invoice, _, _ = _save_invoice_for_matching(
        vendor=vendor,
        purchase_order=po,
        extracted_data={"total": "1800.00"},
    )

    assert invoice.po_id == po.id
    assert invoice.match_status == "match_issues"


def test_invoice_matching_keeps_existing_one_cent_tolerance():
    vendor = SimpleNamespace(id=uuid.uuid4())
    po = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=vendor.id,
        currency="USD",
        total_amount=1500,
    )

    invoice, _, _ = _save_invoice_for_matching(
        vendor=vendor,
        purchase_order=po,
        extracted_data={"total": "1500.01"},
    )

    assert invoice.match_status == "ready_for_review"


def test_rematch_waiting_invoice_when_po_arrives(monkeypatch):

    po_id = uuid.uuid4()
    vendor_id = uuid.uuid4()

    po = SimpleNamespace(
        id=po_id,
        po_number="PO-101",
        vendor_id=vendor_id,
        currency="USD",
        total_amount=1500,
    )
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        po_number_raw="PO-101",
        is_current=True,
        match_status="waiting_for_po",
        vendor_id=vendor_id,
        currency="USD",
        total_amount=1500,
        po_id=None,
    )

    db = MagicMock()

    def auto_match(_db, invoice_id, user_id):
        assert invoice_id == invoice.id
        invoice.match_status = "ready_for_review"
        invoice.final_status = "approved"
        return SimpleNamespace(result="match_ok")

    monkeypatch.setattr(document_routers, "run_match_for_invoice", auto_match)
    (
        db.query.return_value.filter.return_value.with_for_update.return_value.all
    ).return_value = [invoice]

    count = document_routers._rematch_waiting_invoices_for_po(db, po)

    assert count == 1
    assert invoice.po_id == po_id
    assert invoice.match_status == "ready_for_review"
    assert invoice.final_status == "approved"
    db.flush.assert_called_once()


def test_rematch_waiting_invoice_flags_amount_mismatch():
    po = SimpleNamespace(
        id=uuid.uuid4(),
        po_number="PO-101",
        vendor_id=uuid.uuid4(),
        currency="USD",
        total_amount=1500,
    )
    invoice = SimpleNamespace(
        po_number_raw="PO-101",
        is_current=True,
        match_status="waiting_for_po",
        vendor_id=po.vendor_id,
        currency="USD",
        total_amount=1800,
        po_id=None,
    )

    db = MagicMock()
    (
        db.query.return_value.filter.return_value.with_for_update.return_value.all
    ).return_value = [invoice]

    count = document_routers._rematch_waiting_invoices_for_po(db, po)

    assert count == 1
    assert invoice.po_id == po.id
    assert invoice.match_status == "match_issues"


def test_invoice_matching_requires_vendor_review_when_unresolved():
    po = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        currency="USD",
        total_amount=1500,
    )

    invoice, _, _ = _save_invoice_for_matching(
        vendor=None,
        purchase_order=po,
    )

    assert invoice.po_id == po.id
    assert invoice.match_status == "needs_vendor_review"


def test_invoice_matching_requires_review_for_ambiguous_vendor_alias():
    first_vendor = SimpleNamespace(id=uuid.uuid4())
    second_vendor = SimpleNamespace(id=uuid.uuid4())
    po = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=first_vendor.id,
        currency="USD",
        total_amount=1500,
    )

    invoice, _, _ = _save_invoice_for_matching(
        vendor=None,
        purchase_order=po,
        alias_matches=[
            (SimpleNamespace(), first_vendor),
            (SimpleNamespace(), second_vendor),
        ],
    )

    assert invoice.vendor_id is None
    assert invoice.match_status == "needs_vendor_review"


def test_invoice_matching_rerun_rejects_changed_extraction_without_overwrite():
    vendor = SimpleNamespace(id=uuid.uuid4())
    po = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=vendor.id,
        currency="USD",
        total_amount=1500,
    )
    existing_invoice = SimpleNamespace(
        is_current=True,
        invoice_no="INV-101",
        vendor_id=vendor.id,
        vendor_name_raw="ABC Supplies",
        vendor_tax_id_raw=None,
        invoice_date=date(2026, 10, 1),
        due_date=None,
        currency="USD",
        subtotal=Decimal("1500.00"),
        tax_amount=Decimal("0"),
        total_amount=Decimal("1500.00"),
        po_number_raw="PO-101",
        po_id=po.id,
        match_status="ready_for_review",
    )

    with pytest.raises(ValueError, match="differs from the previously saved record"):
        _save_invoice_for_matching(
            vendor=vendor,
            purchase_order=po,
            existing_invoice=existing_invoice,
            extracted_data={"total": "1600.00"},
        )

    assert existing_invoice.po_id == po.id
    assert existing_invoice.match_status == "ready_for_review"
    assert existing_invoice.total_amount == Decimal("1500.00")


def test_invoice_persistence_creates_next_version_and_demotes_current():
    vendor = SimpleNamespace(id=uuid.uuid4())
    prior_historical = SimpleNamespace(version_no=1, is_current=False)
    prior_current = SimpleNamespace(version_no=2, is_current=True)

    invoice, db, _ = _save_invoice_for_matching(
        vendor=vendor,
        purchase_order=None,
        prior_versions=[prior_historical, prior_current],
    )

    assert invoice.version_no == 3
    assert invoice.is_current is True
    assert invoice.vendor_id == vendor.id
    assert invoice.invoice_no == "INV-101"
    assert prior_historical.is_current is False
    assert prior_current.is_current is False
    assert db.add.call_count == 1
    db.flush.assert_called()


def test_identical_current_invoice_is_blocked_as_duplicate():
    vendor = SimpleNamespace(id=uuid.uuid4())
    duplicate = SimpleNamespace(
        id=uuid.uuid4(),
        is_current=True,
        version_no=1,
        invoice_no="INV-101",
        vendor_id=vendor.id,
        vendor_name_raw="ABC Supplies",
        vendor_tax_id_raw=None,
        invoice_date=date(2026, 10, 1),
        due_date=None,
        currency="USD",
        subtotal=Decimal("1500.00"),
        tax_amount=Decimal("0"),
        total_amount=Decimal("1500.00"),
        po_number_raw="PO-101",
        po_id=None,
        match_status="waiting_for_po",
    )
    with pytest.raises(document_routers.DuplicateInvoiceError):
        _save_invoice_for_matching(
            vendor=vendor, purchase_order=None, prior_versions=[duplicate]
        )


def test_corrected_invoice_is_saved_as_next_version():
    vendor = SimpleNamespace(id=uuid.uuid4())
    current = SimpleNamespace(
        id=uuid.uuid4(),
        is_current=True,
        version_no=1,
        invoice_no="INV-101",
        vendor_id=vendor.id,
        vendor_name_raw="ABC Supplies",
        vendor_tax_id_raw=None,
        invoice_date=date(2026, 10, 1),
        due_date=None,
        currency="USD",
        subtotal=Decimal("1500.00"),
        tax_amount=Decimal("0"),
        total_amount=Decimal("1500.00"),
        po_number_raw="PO-101",
        po_id=None,
        match_status="waiting_for_po",
    )
    invoice, _, _ = _save_invoice_for_matching(
        vendor=vendor,
        purchase_order=None,
        prior_versions=[current],
        extracted_data={"total": "1501.00", "subtotal": "1501.00"},
    )
    assert invoice.version_no == 2
    assert current.is_current is False


def test_invoice_persistence_skips_version_matching_without_invoice_number():
    vendor = SimpleNamespace(id=uuid.uuid4())

    invoice, db, _ = _save_invoice_for_matching(
        vendor=vendor,
        purchase_order=None,
        prior_versions=[SimpleNamespace(version_no=1, is_current=True)],
        extracted_data={"invoice_number": ""},
    )

    assert invoice.invoice_no is None
    assert invoice.version_no == 1
    assert invoice.is_current is True
    db.version_query.filter.assert_not_called()


def test_invoice_persistence_skips_version_matching_without_vendor():
    invoice, db, _ = _save_invoice_for_matching(
        vendor=None,
        purchase_order=None,
        prior_versions=[SimpleNamespace(version_no=1, is_current=True)],
    )

    assert invoice.vendor_id is None
    assert invoice.version_no == 1
    assert invoice.is_current is True
    db.version_query.filter.assert_not_called()


def test_invoice_persistence_rerun_updates_current_source_without_new_version():
    vendor = SimpleNamespace(id=uuid.uuid4())
    existing_invoice = SimpleNamespace(
        id=uuid.uuid4(),
        is_current=True,
        version_no=4,
        invoice_no="INV-101",
        vendor_id=vendor.id,
        vendor_name_raw="ABC Supplies",
        vendor_tax_id_raw=None,
        invoice_date=date(2026, 10, 1),
        due_date=None,
        currency="USD",
        subtotal=Decimal("1500.00"),
        tax_amount=Decimal("0"),
        total_amount=Decimal("1500.00"),
        po_number_raw="PO-101",
        po_id=None,
        match_status="waiting_for_po",
    )

    invoice, db, _ = _save_invoice_for_matching(
        vendor=vendor,
        purchase_order=None,
        existing_invoice=existing_invoice,
        prior_versions=[SimpleNamespace(version_no=3, is_current=False)],
    )

    assert invoice is existing_invoice
    assert invoice.version_no == 4
    assert invoice.is_current is True
    assert invoice.invoice_no == "INV-101"
    db.add.assert_not_called()
    db.version_query.filter.assert_not_called()


def test_parse_document_persists_valid_purchase_order(monkeypatch):
    from datetime import date
    from decimal import Decimal

    from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine

    vendor = SimpleNamespace(id=uuid.uuid4())
    data = {
        "document_type": "po",
        "vendor": "ABC Supplies",
        "po_number": "PO-101",
        "po_date": "2026-10-01",
        "currency": "usd",
        "subtotal": "100.00",
        "tax": "10.00",
        "total": "110.00",
        "payment_terms": "Net 30",
        "delivery_details": {"address": "Receiving dock"},
        "line_items": [
            {
                "item_code": "ITEM-1",
                "description": "Office supplies",
                "qty": "2",
                "uom": "EA",
                "unit_price": "50.00",
                "line_amount": "100.00",
            }
        ],
    }

    response, document, db = _run_purchase_order_parse(
        monkeypatch,
        data,
        vendor=vendor,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "extracted"
    assert document.doc_type == "po"
    assert document.extracted_json == data
    assert db.add.call_count == 2
    purchase_order = next(
        record
        for record in (call.args[0] for call in db.add.call_args_list)
        if isinstance(record, PurchaseOrder)
    )
    assert isinstance(purchase_order, PurchaseOrder)
    assert purchase_order.po_number == "PO-101"
    assert purchase_order.vendor_id == vendor.id
    assert purchase_order.po_date == date(2026, 10, 1)
    assert purchase_order.currency == "USD"
    assert purchase_order.subtotal == Decimal("100.00")
    assert purchase_order.tax_amount == Decimal("10.00")
    assert purchase_order.total_amount == Decimal("110.00")
    assert purchase_order.source_document_id == document.id
    assert purchase_order.payment_terms == "Net 30"
    assert purchase_order.delivery_details == {"address": "Receiving dock"}
    purchase_order_line = next(
        record
        for record in (call.args[0] for call in db.add.call_args_list)
        if isinstance(record, PurchaseOrderLine)
    )
    assert purchase_order_line.item_code == "ITEM-1"
    assert purchase_order_line.qty == Decimal("2")
    assert purchase_order_line.created_by == document.created_by


def test_parse_document_resolves_purchase_order_vendor_alias(monkeypatch):
    from src.models.purchase_order_models import PurchaseOrder

    vendor = SimpleNamespace(id=uuid.uuid4())
    data = {
        "vendor": "  Alternate   Supplier ",
        "po_number": "PO-ALT-1",
        "po_date": "2026-10-01",
        "currency": "usd",
        "subtotal": "100.00",
        "tax": "10.00",
        "total": "110.00",
    }

    response, _, db = _run_purchase_order_parse(
        monkeypatch,
        data,
        alias_vendor=vendor,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "extracted"
    purchase_order = db.add.call_args.args[0]
    assert isinstance(purchase_order, PurchaseOrder)
    assert purchase_order.vendor_id == vendor.id


def test_parse_document_does_not_persist_incomplete_purchase_order(
    monkeypatch,
):
    data = {
        "vendor": "ABC Supplies",
        "po_number": "PO-101",
        "currency": "USD",
        "total": "110.00",
    }

    response, document, db = _run_purchase_order_parse(
        monkeypatch,
        data,
        vendor=SimpleNamespace(id=uuid.uuid4()),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "needs_vendor_review"
    assert "Missing required field: po_date" in response.json()["validation_issues"]
    assert document.extracted_json == data
    db.add.assert_not_called()


def test_parse_document_flags_duplicate_current_purchase_order(
    monkeypatch,
):
    duplicate_id = uuid.uuid4()
    duplicate = SimpleNamespace(id=duplicate_id)
    data = {
        "vendor": "ABC Supplies",
        "po_number": "PO-101",
        "po_date": "2026-10-01",
        "currency": "USD",
        "total": "110.00",
    }

    response, document, db = _run_purchase_order_parse(
        monkeypatch,
        data,
        vendor=SimpleNamespace(id=uuid.uuid4()),
        duplicate=duplicate,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "needs_vendor_review"
    assert response.json()["duplicate_of"] == str(duplicate_id)
    assert any(
        "Duplicate current purchase order number" in issue
        for issue in response.json()["validation_issues"]
    )
    assert document.status == "needs_vendor_review"
    db.add.assert_not_called()


def test_parse_document_requires_known_vendor_for_purchase_order(
    monkeypatch,
):
    data = {
        "vendor": "Unknown Supplies",
        "po_number": "PO-101",
        "po_date": "2026-10-01",
        "currency": "USD",
        "total": "110.00",
    }

    response, document, db = _run_purchase_order_parse(
        monkeypatch,
        data,
        vendor=None,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "needs_vendor_review"
    assert (
        "Vendor not found or inactive: Unknown Supplies"
        in response.json()["validation_issues"]
    )
    assert document.extracted_json == data
    db.add.assert_not_called()


def test_parse_document_flags_po_line_subtotal_mismatch(monkeypatch):
    data = {
        "vendor": "ABC Supplies",
        "po_number": "PO-102",
        "po_date": "2026-10-01",
        "currency": "USD",
        "subtotal": "100.00",
        "tax": "10.00",
        "total": "110.00",
        "line_items": [
            {"qty": "1", "line_amount": "90.00"},
        ],
    }

    response, document, db = _run_purchase_order_parse(
        monkeypatch,
        data,
        vendor=SimpleNamespace(id=uuid.uuid4()),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "needs_vendor_review"
    assert (
        "PO line amounts do not reconcile to subtotal"
        in response.json()["validation_issues"]
    )
    assert document.status == "needs_vendor_review"
    db.add.assert_not_called()


def test_parse_document_not_found(monkeypatch):
    from src.db.session_db import get_db

    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value.first.return_value = None
    locked_query = query.filter.return_value.with_for_update.return_value
    locked_query.first.return_value = None

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4())}

    try:
        response = client.post(f"/api/documents/{uuid.uuid4()}/parse")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404


def test_parse_document_download_failure(monkeypatch):
    from src.db.session_db import get_db

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
    query = db.query.return_value
    query.filter.return_value.first.return_value = document
    locked_query = query.filter.return_value.with_for_update.return_value
    locked_query.first.return_value = document

    def fail_download(path):
        raise RuntimeError("MinIO download failed")

    monkeypatch.setattr(
        document_routers,
        "download_file",
        fail_download,
    )

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4())}

    try:
        response = client.post(f"/api/documents/{document_id}/parse")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code >= 400


def test_parse_document_classification_failure(monkeypatch):
    from src.db.session_db import get_db

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
    query = db.query.return_value
    query.filter.return_value.first.return_value = document
    locked_query = query.filter.return_value.with_for_update.return_value
    locked_query.first.return_value = document

    monkeypatch.setattr(
        document_routers,
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
        document_routers,
        "classify_document",
        fail_classification,
    )

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4())}

    try:
        response = client.post(f"/api/documents/{document_id}/parse")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code >= 400


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("Password required to open PDF", "password-protected"),
        ("Corrupt PDF: unexpected EOF", "appears damaged"),
        ("Unsupported input format", "unsupported"),
    ],
)
def test_parse_failure_has_plain_language_reason(error, expected):
    message = document_routers._document_failure_message(RuntimeError(error))
    assert expected in message


def test_parse_marks_unsupported_stored_file_failed(monkeypatch):
    from src.db.session_db import get_db

    document = SimpleNamespace(
        id=uuid.uuid4(),
        original_filename="unsupported.exe",
        storage_path="documents/unsupported.exe",
        status="received",
        failure_message=None,
        email_sender=None,
    )
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value.with_for_update.return_value.first.return_value = document
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4())}
    try:
        response = client.post(f"/api/documents/{document.id}/parse")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 200
    assert document.status == "failed"
    assert "unsupported" in document.failure_message


def test_parse_document_extraction_failure(monkeypatch):
    from src.db.session_db import get_db

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
    query = db.query.return_value
    query.filter.return_value.first.return_value = document
    locked_query = query.filter.return_value.with_for_update.return_value
    locked_query.first.return_value = document

    monkeypatch.setattr(
        document_routers,
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
        document_routers,
        "classify_document",
        lambda text: "invoice",
    )

    def fail_extraction(text, doc_type):
        raise RuntimeError("Extraction failed")

    monkeypatch.setattr(
        document_routers,
        "extract_document",
        fail_extraction,
    )

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4())}

    try:
        response = client.post(f"/api/documents/{document_id}/parse")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code >= 400
