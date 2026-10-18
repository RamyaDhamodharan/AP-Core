
import sys
import uuid
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.routers import document_routers

client = TestClient(app)


def _save_invoice_for_matching(
    *,
    vendor,
    purchase_order,
    existing_invoice=None,
    extracted_data=None,
):
    from src.models.invoice_models import Invoice
    from src.models.purchase_order_models import PurchaseOrder
    from src.models.vendor_models import Vendor

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

    invoice_query = MagicMock()
    invoice_query.filter.return_value.first.return_value = existing_invoice
    vendor_query = MagicMock()
    vendor_query.filter.return_value.first.return_value = vendor
    po_query = MagicMock()
    po_query.filter.return_value.first.return_value = purchase_order
    db = MagicMock()
    db.query.side_effect = {
        Invoice: invoice_query,
        Vendor: vendor_query,
        PurchaseOrder: po_query,
    }.__getitem__

    invoice = document_routers._save_invoice_from_extraction(
        db,
        document,
        data,
    )
    return invoice, db, document


def _run_purchase_order_parse(monkeypatch, extracted_data, vendor=None, duplicate=None):
    from src.db.session_db import get_db
    from src.models.document_models import Document
    from src.models.purchase_order_models import PurchaseOrder
    from src.models.vendor_models import Vendor

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
    )

    document_query = MagicMock()
    document_query.filter_by.return_value.first.return_value = document
    vendor_query = MagicMock()
    vendor_query.filter.return_value.first.return_value = vendor
    purchase_order_query = MagicMock()
    purchase_order_query.filter.return_value.first.return_value = duplicate

    db = MagicMock()

    def query_model(model):
        return {
            Document: document_query,
            Vendor: vendor_query,
            PurchaseOrder: purchase_order_query,
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

    try:
        response = client.post(
            f"/api/documents/{document_id}/parse"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

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
    db.query.return_value.filter_by.return_value.first.return_value = document

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
}
    )

    # Override FastAPI database dependency
    from src.db.session_db import get_db

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
        "document_type": "invoice",
        "vendor": "ABC Supplies",
        "invoice_number": "INV-101",
        "invoice_date": "2026-10-01",
        "subtotal": 1500,
        "tax": 0,
        "total": 1500,
        "line_items": [],
    }


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


def test_invoice_matching_updates_existing_record_on_rerun():
    vendor = SimpleNamespace(id=uuid.uuid4())
    po = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=vendor.id,
        currency="USD",
        total_amount=1500,
    )
    existing_invoice = SimpleNamespace()

    invoice, db, _ = _save_invoice_for_matching(
        vendor=vendor,
        purchase_order=po,
        existing_invoice=existing_invoice,
        extracted_data={"total": "1600.00"},
    )

    assert invoice is existing_invoice
    assert invoice.po_id == po.id
    assert invoice.match_status == "match_issues"
    assert invoice.total_amount == 1600
    db.add.assert_not_called()


def test_parse_document_persists_valid_purchase_order(monkeypatch):
    from datetime import date
    from decimal import Decimal

    from src.models.purchase_order_models import PurchaseOrder

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
    db.add.assert_called_once()
    purchase_order = db.add.call_args.args[0]
    assert isinstance(purchase_order, PurchaseOrder)
    assert purchase_order.po_number == "PO-101"
    assert purchase_order.vendor_id == vendor.id
    assert purchase_order.po_date == date(2026, 10, 1)
    assert purchase_order.currency == "USD"
    assert purchase_order.subtotal == Decimal("100.00")
    assert purchase_order.tax_amount == Decimal("10.00")
    assert purchase_order.total_amount == Decimal("110.00")
    assert purchase_order.source_document_id == document.id


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
    assert "Missing required field: po_date" in response.json()[
        "validation_issues"
    ]
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


def test_parse_document_not_found(monkeypatch):
    from src.db.session_db import get_db

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
    db.query.return_value.filter_by.return_value.first.return_value = document

    def fail_download(path):
        raise RuntimeError("MinIO download failed")

    monkeypatch.setattr(
        document_routers,
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
    db.query.return_value.filter_by.return_value.first.return_value = document

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

    try:
        response = client.post(
            f"/api/documents/{document_id}/parse"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code >= 400


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
    db.query.return_value.filter_by.return_value.first.return_value = document

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

    try:
        response = client.post(
            f"/api/documents/{document_id}/parse"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code >= 400

def test_duplicate_invoice_detected_for_same_vendor_and_number():
    existing_invoice = SimpleNamespace(
        id=uuid.uuid4(),
        extracted_json={
            "vendor": "ABC Supplies",
            "invoice_number": "INV-101",
        },
    )

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [
        existing_invoice
    ]

    new_document = SimpleNamespace(
        id=uuid.uuid4(),
        doc_type="invoice",
    )

    result = document_routers._find_duplicate_invoice(
        db,
        new_document,
        {
            "vendor": " abc supplies ",
            "invoice_number": "inv-101",
        },
    )

    assert result is existing_invoice


def test_duplicate_invoice_not_detected_for_different_vendor():
    existing_invoice = SimpleNamespace(
        id=uuid.uuid4(),
        extracted_json={
            "vendor": "ABC Supplies",
            "invoice_number": "INV-101",
        },
    )

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [
        existing_invoice
    ]

    new_document = SimpleNamespace(
        id=uuid.uuid4(),
        doc_type="invoice",
    )

    result = document_routers._find_duplicate_invoice(
        db,
        new_document,
        {
            "vendor": "XYZ Traders",
            "invoice_number": "INV-101",
        },
    )

    assert result is None


def test_duplicate_invoice_skipped_when_invoice_number_missing():
    db = MagicMock()

    new_document = SimpleNamespace(
        id=uuid.uuid4(),
        doc_type="invoice",
    )

    result = document_routers._find_duplicate_invoice(
        db,
        new_document,
        {
            "vendor": "ABC Supplies",
            "invoice_number": "",
        },
    )

    assert result is None
    db.query.assert_not_called()


def test_duplicate_invoice_skipped_for_purchase_order():
    db = MagicMock()

    new_document = SimpleNamespace(
        id=uuid.uuid4(),
        doc_type="po",
    )

    result = document_routers._find_duplicate_invoice(
        db,
        new_document,
        {
            "vendor": "ABC Supplies",
            "invoice_number": "INV-101",
        },
    )

    assert result is None
    db.query.assert_not_called()

def test_parse_document_flags_duplicate_invoice(monkeypatch):
    from src.db.session_db import get_db

    document_id = uuid.uuid4()
    existing_id = uuid.uuid4()

    document = SimpleNamespace(
        id=document_id,
        original_filename="invoice-copy.pdf",
        storage_path=f"documents/{document_id}.pdf",
        status="received",
        doc_type=None,
        raw_text=None,
        extracted_json=None,
        failure_message=None,
    )

    existing_invoice = SimpleNamespace(
        id=existing_id,
        extracted_json={
            "vendor": "ABC Supplies",
            "invoice_number": "INV-101",
        },
    )

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = (
        document
    )
    db.query.return_value.filter.return_value.all.return_value = [
        existing_invoice
    ]

    monkeypatch.setattr(
        document_routers,
        "download_file",
        lambda path: b"fake PDF content",
    )

    class FakeConverter:
        def convert(self, path):
            return SimpleNamespace(
                document=SimpleNamespace(
                    export_to_markdown=lambda: (
                        "Invoice INV-101 from ABC Supplies. Total 1500"
                    )
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

    monkeypatch.setattr(
        document_routers,
        "validate_extracted_data",
        lambda data, doc_type: [],
    )

    app.dependency_overrides[get_db] = lambda: db

    try:
        response = client.post(
            f"/api/documents/{document_id}/parse"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert response.json()["status"] == "needs_vendor_review"
    assert response.json()["duplicate_of"] == str(existing_id)
    assert document.status == "needs_vendor_review"
