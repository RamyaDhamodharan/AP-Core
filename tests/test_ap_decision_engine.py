from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from src.models.invoice_models import InvoiceLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.services import ap_decision_engine as engine


def _fixture(monkeypatch, *, po_count=1, receipt=True, duplicate=False):
    vendor_id = "vendor-1"
    po_line = SimpleNamespace(
        id="pl-1",
        item_code="SKU-1",
        description="Widget",
        qty=Decimal(10),
        unit_price=Decimal(10),
        line_amount=Decimal(100),
    )
    po = SimpleNamespace(
        id="po-1",
        po_number="PO-1",
        vendor_id=vendor_id,
        vendor_name="Vendor One",
        vendor=SimpleNamespace(tax_id="TAX-1"),
        currency="USD",
        tax_amount=Decimal(0),
        total_amount=Decimal(100),
    )
    gr = SimpleNamespace(id="gr-1", gr_number="GR-1", receipt_date=None)
    gr_line = SimpleNamespace(
        id="grl-1",
        po_line_id="pl-1",
        item_code="SKU-1",
        description="Widget",
        received_qty=Decimal(10),
        accepted_qty=Decimal(10),
        rejected_qty=Decimal(0),
    )
    line = SimpleNamespace(
        item_code="SKU-1",
        description="Widget",
        qty=Decimal(10),
        unit_price=Decimal(10),
        line_amount=Decimal(100),
        field_confidence={
            key: {"confidence": 85} for key in ("qty", "unit_price", "line_amount")
        },
    )
    invoice = SimpleNamespace(
        id="inv-1",
        po_number_raw="PO-1",
        po_id=None,
        vendor_id=vendor_id,
        vendor_name_raw="Vendor One",
        vendor_tax_id_raw="TAX-1",
        invoice_no="INV-1",
        invoice_no_normalized="INV1",
        currency="USD",
        subtotal=Decimal(100),
        tax_amount=Decimal(0),
        total_amount=Decimal(100),
        field_confidence={key: {"confidence": 85} for key in engine.MATERIAL_FIELDS},
    )
    datasets = {
        PurchaseOrder: [po] * po_count,
        PurchaseOrderLine: [po_line],
        GoodsReceipt: [gr] if receipt else [],
        GoodsReceiptLine: [gr_line] if receipt else [],
        InvoiceLine: [line],
    }
    db = MagicMock()

    def query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.all.return_value = datasets.get(model, [])
        return q

    db.query.side_effect = query
    monkeypatch.setattr(
        engine,
        "find_duplicate_invoice",
        lambda *_a, **_k: object() if duplicate else None,
    )
    return db, invoice, po, line


def _codes(monkeypatch, mutate=lambda _i, _p, _l: None, **kwargs):
    db, invoice, po, line = _fixture(monkeypatch, **kwargs)
    mutate(invoice, po, line)
    return {
        item["code"] for item in engine.evaluate_invoice(db, invoice)["discrepancies"]
    }


@pytest.mark.parametrize(
    ("code", "mutate", "options"),
    [
        (
            "PO_CONFIRMATION_REQUIRED",
            lambda invoice, purchase_order, line: setattr(
                invoice, "po_number_raw", None
            ),
            {},
        ),
        ("AMBIGUOUS_PO", lambda invoice, purchase_order, line: None, {"po_count": 2}),
        (
            "VENDOR_MISMATCH",
            lambda invoice, purchase_order, line: (
                setattr(invoice, "vendor_id", "other"),
                setattr(invoice, "vendor_tax_id_raw", "OTHER-TAX"),
            ),
            {},
        ),
        ("MISSING_GR", lambda invoice, purchase_order, line: None, {"receipt": False}),
        (
            "LINE_NOT_ON_PO",
            lambda invoice, purchase_order, line: setattr(line, "item_code", "other"),
            {},
        ),
        (
            "QUANTITY_VARIANCE",
            lambda invoice, purchase_order, line: setattr(line, "qty", Decimal(11)),
            {},
        ),
        (
            "PRICE_VARIANCE",
            lambda invoice, purchase_order, line: setattr(
                line, "unit_price", Decimal("10.21")
            ),
            {},
        ),
        (
            "LINE_TOTAL_VARIANCE",
            lambda invoice, purchase_order, line: setattr(
                line, "line_amount", Decimal(99)
            ),
            {},
        ),
        (
            "TAX_VARIANCE",
            lambda invoice, purchase_order, line: (
                setattr(invoice, "tax_amount", Decimal(1)),
                setattr(invoice, "total_amount", Decimal(101)),
            ),
            {},
        ),
        (
            "HEADER_TOTAL_VARIANCE",
            lambda invoice, purchase_order, line: setattr(
                invoice, "total_amount", Decimal(101)
            ),
            {},
        ),
        (
            "CURRENCY_MISMATCH",
            lambda invoice, purchase_order, line: setattr(invoice, "currency", "EUR"),
            {},
        ),
        (
            "POSSIBLE_DUPLICATE",
            lambda invoice, purchase_order, line: None,
            {"duplicate": True},
        ),
        (
            "LOW_CONFIDENCE",
            lambda invoice, purchase_order, line: invoice.field_confidence[
                "total"
            ].update(confidence=84),
            {},
        ),
    ],
)
def test_each_rule_reason_code(monkeypatch, code, mutate, options):
    assert code in _codes(monkeypatch, mutate, **options)


def test_ready_to_pay_requires_exact_two_percent_and_85_confidence(monkeypatch):
    db, invoice, _po, line = _fixture(monkeypatch)
    line.unit_price = Decimal("10.20")
    line.line_amount = Decimal("102")
    invoice.subtotal = Decimal("102")
    invoice.total_amount = Decimal("102")
    outcome = engine.evaluate_invoice(db, invoice)
    assert outcome["outcome"] == "ready_to_pay"


def test_po_prefix_and_leading_zero_variants_match_the_saved_po(monkeypatch):
    db, invoice, po, _line = _fixture(monkeypatch)
    invoice.po_number_raw = " PO - 0001 "
    po.po_number = "1"
    outcome = engine.evaluate_invoice(db, invoice)
    assert outcome["inputs_summary"]["po_ids"] == [str(po.id)]
    assert outcome["outcome"] == "ready_to_pay"


def test_unresolved_vendor_is_a_saved_blocking_reason(monkeypatch):
    db, invoice, _po, _line = _fixture(monkeypatch)
    invoice.vendor_id = None
    invoice.vendor_tax_id_raw = None
    outcome = engine.evaluate_invoice(db, invoice)
    assert outcome["outcome"] == "needs_review"
    assert "VENDOR_UNRESOLVED" in {
        item["code"] for item in outcome["discrepancies"]
    }


def test_multiple_discrepancies_are_kept(monkeypatch):
    def mutate(invoice, _po, line):
        line.qty = Decimal(12)
        line.unit_price = Decimal("10.50")
        line.line_amount = Decimal(120)
        invoice.currency = "EUR"

    codes = _codes(monkeypatch, mutate)
    assert {"QUANTITY_VARIANCE", "PRICE_VARIANCE", "CURRENCY_MISMATCH"} <= codes


@pytest.mark.parametrize(("setting", "expected"), [("true", True), ("false", False)])
def test_goods_receipt_requirement_flag(monkeypatch, setting, expected):
    monkeypatch.setenv("REQUIRE_GOODS_RECEIPT", setting)
    db, invoice, _po, _line = _fixture(monkeypatch, receipt=False)
    codes = {
        item["code"] for item in engine.evaluate_invoice(db, invoice)["discrepancies"]
    }
    assert ("MISSING_GR" in codes) is expected
    assert ("QUANTITY_VARIANCE" in codes) is expected


def test_match_payload_saves_full_comparison_and_price_mismatch(monkeypatch):
    db, invoice, _po, line = _fixture(monkeypatch)
    line.unit_price = Decimal("10.21")
    line.line_amount = Decimal("102.10")
    invoice.total_amount = invoice.subtotal = Decimal("102.10")
    result = engine.evaluate_invoice(db, invoice)
    comparison = result["comparison"]
    assert {row["field"] for row in comparison["headers"]} >= {
        "vendor_name",
        "vendor_tax_id",
        "po_number",
        "currency",
        "dates",
        "payment_terms",
        "subtotal",
        "tax_amount",
        "total_amount",
        "goods_receipt_count",
    }
    headers = {row["field"]: row for row in comparison["headers"]}
    assert headers["vendor_name"]["invoice_value"] == "Vendor One"
    assert headers["vendor_name"]["po_value"] == "Vendor One"
    assert headers["vendor_tax_id"]["gr_value"] is None
    assert headers["currency"]["invoice_value"] == "USD"
    assert headers["goods_receipt_count"]["gr_value"] == 1
    assert headers["total_amount"]["status"] == "mismatch"
    assert headers["total_amount"]["reason_code"] == "HEADER_TOTAL_VARIANCE"
    assert comparison["lines"][0]["unit_price"]["invoice"] == "10.21"
    assert comparison["lines"][0]["unit_price"]["po"] == "10"
    assert comparison["lines"][0]["reason_code"] == "PRICE_VARIANCE"
    assert comparison["lines"][0]["difference"]["unit_price"] == "0.21"


def test_comparison_payload_reports_gr_quantity_and_missing_receipt(monkeypatch):
    db, invoice, _po, line = _fixture(monkeypatch)
    line.qty = Decimal("11")
    result = engine.evaluate_invoice(db, invoice)
    comparison = result["comparison"]
    assert comparison["lines"][0]["quantity"]["gr_accepted"] == "10"
    assert comparison["lines"][0]["reason_code"] == "QUANTITY_VARIANCE"

    db, invoice, _po, _line = _fixture(monkeypatch, receipt=False)
    missing = engine.evaluate_invoice(db, invoice)["comparison"]
    gr_row = next(
        row for row in missing["headers"] if row["field"] == "goods_receipt_count"
    )
    assert gr_row["gr_value"] == 0
    assert gr_row["status"] == "mismatch"
    assert gr_row["reason_code"] == "MISSING_GR"


def test_comparison_payload_marks_goods_receipt_not_required(monkeypatch):
    monkeypatch.setenv("REQUIRE_GOODS_RECEIPT", "false")
    db, invoice, _po, _line = _fixture(monkeypatch, receipt=False)
    result = engine.evaluate_invoice(db, invoice)
    gr_row = next(
        row
        for row in result["comparison"]["headers"]
        if row["field"] == "goods_receipt_count"
    )
    assert gr_row["status"] == "not_required"
    assert gr_row["gr_value"] == 0


@pytest.mark.parametrize(("setting", "expected"), [("true", True), ("false", False)])
def test_currency_conversion_flag(setting, expected, monkeypatch):
    from src.services.runtime_config_services import currency_conversion_enabled

    monkeypatch.setenv("CURRENCY_CONVERSION", setting)
    assert currency_conversion_enabled() is expected
