"""
Deterministic, Decimal-only AP matching decision rules.
Flow: invoice-to-PO and goods-receipt matching.
Called by: src/services/invoice_matching_services.py.
Calls: document_workflow_services, goods_receipt_models,
invoice_confidence_services, invoice_models, invoice_number_services.
Tables: goods_receipt_lines, goods_receipts, invoice_lines, invoices, po_lines,
purchase_orders.
"""

import os
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models.document_models import Document
from src.models.exchange_rate_models import ExchangeRate
from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from src.models.invoice_match_models import InvoiceMatch
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.services.invoice_comparison_services import build_comparison_snapshot
from src.services.invoice_confidence_services import overall_confidence_percent
from src.services.invoice_number_services import find_duplicate_invoice
from src.services.po_matching_services import normalize_po_reference, normalize_value
from src.services.runtime_config_services import (
    currency_conversion_enabled,
    invoice_only_requires_approval,
    require_goods_receipt,
)
from src.services.vendor_resolution_services import normalize_vendor_tax_id

RULE_SET_VERSION = os.getenv("AP_MATCH_RULE_SET_VERSION", "ap-match-v1")
PRICE_TOLERANCE = Decimal(os.getenv("AP_PRICE_TOLERANCE_PERCENT", "2")) / 100
TAX_TOLERANCE = Decimal(os.getenv("TAX_TOLERANCE", "0.01"))
MIN_MATERIAL_CONFIDENCE = Decimal(os.getenv("AP_MATCH_MIN_CONFIDENCE", "85"))
MATERIAL_FIELDS = (
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


def _d(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _score(raw):
    value = raw.get("confidence") if isinstance(raw, dict) else raw
    try:
        score = Decimal(str(value))
        return score * 100 if Decimal(0) <= score <= Decimal(1) else score
    except Exception:
        return None


def _issue(
    code,
    message,
    actual=None,
    expected=None,
    difference=None,
    tolerance=None,
    line=None,
    action="Review the invoice and supporting records.",
):
    return {
        "code": code,
        "message": message,
        "invoice_value": actual,
        "expected_value": expected,
        "difference": difference,
        "tolerance": tolerance,
        "line_reference": line,
        "next_action": action,
    }


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _stored_exchange_rate(db, invoice: Invoice, po: PurchaseOrder):
    """Find the latest usable direct or inverse rate for the invoice date."""
    from_currency = str(getattr(invoice, "currency", None) or "").strip().upper()
    to_currency = str(getattr(po, "currency", None) or "").strip().upper()
    if not from_currency or not to_currency or from_currency == to_currency:
        return None
    rate_date = getattr(invoice, "invoice_date", None)
    date_filter = (ExchangeRate.rate_date <= rate_date,) if rate_date else ()
    direct = (
        db.query(ExchangeRate)
        .filter(
            ExchangeRate.from_currency == from_currency,
            ExchangeRate.to_currency == to_currency,
            *date_filter,
        )
        .order_by(ExchangeRate.rate_date.desc(), ExchangeRate.created_at.desc())
        .first()
    )
    if direct is not None:
        rate = _d(getattr(direct, "rate", None))
        if rate is not None and rate > 0:
            return direct, rate
    inverse = (
        db.query(ExchangeRate)
        .filter(
            ExchangeRate.from_currency == to_currency,
            ExchangeRate.to_currency == from_currency,
            *date_filter,
        )
        .order_by(ExchangeRate.rate_date.desc(), ExchangeRate.created_at.desc())
        .first()
    )
    inverse_rate = _d(getattr(inverse, "rate", None)) if inverse is not None else None
    if inverse is not None and inverse_rate is not None and inverse_rate > 0:
        return inverse, Decimal("1") / inverse_rate
    return None


def evaluate_invoice(db: Session, invoice: Invoice, owner_id=None) -> dict:
    """
    Return a complete saved-ready outcome; descriptions never auto-link lines.
    Inputs: db (Session), invoice (Invoice), owner_id.
    Output: dict.
    Effects: reads database rows.
    Results: reason codes AMBIGUOUS_PO, CURRENCY_MISMATCH, HEADER_TOTAL_VARIANCE,
        LINE_NOT_ON_PO, LINE_TOTAL_VARIANCE, LOW_CONFIDENCE, MISSING_GR, MISSING_PO;
        statuses needs_review, ready_to_pay.
    """
    # Step 1: Resolve only an exact normalized PO number; ambiguous numbers need review.
    issues = []
    po_rows = []
    po_number_raw = getattr(invoice, "po_number_raw", None)
    po_ref = normalize_po_reference(po_number_raw)
    invoice_only = getattr(invoice, "po_decision", None) == "invoice_only"
    if po_ref and not invoice_only:
        po_rows = [
            po
            for po in db.query(PurchaseOrder)
            .filter(PurchaseOrder.is_current.is_(True))
            .all()
            if normalize_po_reference(po.po_number) == po_ref
        ]
        confirmed_match = (
            db.query(PurchaseOrder)
            .join(InvoiceMatch, InvoiceMatch.po_id == PurchaseOrder.id)
            .filter(
                InvoiceMatch.invoice_id == invoice.id,
                InvoiceMatch.status == "confirmed",
                PurchaseOrder.is_current.is_(True),
            )
            .first()
        )
        if confirmed_match is not None and confirmed_match in po_rows:
            po_rows = [confirmed_match]
    elif not invoice_only:
        confirmed_match = (
            db.query(PurchaseOrder)
            .join(InvoiceMatch, InvoiceMatch.po_id == PurchaseOrder.id)
            .filter(
                InvoiceMatch.invoice_id == invoice.id,
                InvoiceMatch.status == "confirmed",
                PurchaseOrder.is_current.is_(True),
            )
            .first()
        )
        if confirmed_match is not None:
            po_rows = [confirmed_match]
    po = po_rows[0] if len(po_rows) == 1 else None
    if po_ref and not po_rows and not invoice_only:
        issues.append(
            _issue(
                "PO_NOT_RECEIVED",
                f"PO {po_number_raw} not received yet",
                po_number_raw,
                None,
                action="Wait for this PO to be received or correct its number.",
            )
        )
    elif len(po_rows) > 1 and not invoice_only:
        issues.append(
            _issue(
                "AMBIGUOUS_PO",
                "More than one purchase order matches this number.",
                po_number_raw,
                len(po_rows),
                action="Resolve the duplicate PO records.",
            )
        )

    # Step 2: Load invoice, PO and receipt lines for quantity and SKU evidence.
    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
    if not lines and po is not None:
        issues.append(
            _issue(
                "LINE_NOT_ON_PO",
                "No invoice lines are available to match against the PO.",
                None,
                "At least one matched PO line",
                action="Verify the extracted invoice line items.",
            )
        )
    po_lines = (
        db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all()
        if po
        else []
    )
    receipts = (
        db.query(GoodsReceipt).filter(GoodsReceipt.po_id == po.id).all() if po else []
    )
    require_receipt = require_goods_receipt()
    if po is not None and not invoice_only and not receipts and require_receipt:
        issues.append(
            _issue(
                "MISSING_GR",
                "No goods receipt is recorded for this PO.",
                None,
                "Goods receipt",
                action="Record or link a goods receipt.",
            )
        )
    received = {}
    receipt_lines = []
    if receipts:
        receipt_lines = (
            db.query(GoodsReceiptLine)
            .filter(GoodsReceiptLine.goods_receipt_id.in_([g.id for g in receipts]))
            .all()
        )
        for grline in receipt_lines:
            received[grline.po_line_id] = received.get(
                grline.po_line_id, Decimal(0)
            ) + _d(grline.accepted_qty)

    # Step 3: Check vendor ownership and matching currencies before comparing amounts.
    invoice_vendor_id = getattr(invoice, "vendor_id", None)
    invoice_tax_id = normalize_vendor_tax_id(
        getattr(invoice, "vendor_tax_id_raw", None)
    )
    po_vendor = getattr(po, "vendor", None) if po is not None else None
    if not invoice_vendor_id:
        issues.append(
            _issue(
                "VENDOR_UNRESOLVED",
                "Invoice vendor is not resolved to an active vendor record.",
                getattr(invoice, "vendor_name_raw", None),
                None,
                action="Resolve the invoice vendor before payment.",
            )
        )
    vendor_matches = bool(
        po and invoice_vendor_id and invoice_vendor_id == po.vendor_id
    )
    vendor_matches = vendor_matches or bool(
        po_vendor
        and invoice_tax_id
        and normalize_vendor_tax_id(po_vendor.tax_id) == invoice_tax_id
    )
    if po and invoice_vendor_id and not vendor_matches:
        issues.append(
            _issue(
                "VENDOR_MISMATCH",
                "Invoice vendor does not match the PO vendor.",
                str(invoice_vendor_id),
                str(po.vendor_id),
                action="Correct the vendor or PO assignment.",
            )
        )
    currency = getattr(invoice, "currency", None)
    exchange_rate = None
    if (
        po
        and currency
        and po.currency
        and currency.upper() != po.currency.upper()
        and currency_conversion_enabled()
    ):
        exchange_rate = _stored_exchange_rate(db, invoice, po)
    if po and (
        not currency
        or not po.currency
        or (currency.upper() != po.currency.upper() and exchange_rate is None)
    ):
        issues.append(
            _issue(
                "CURRENCY_MISMATCH",
                "Invoice and PO currencies must match or have a stored exchange rate.",
                currency,
                po.currency,
                action="Correct the currency or add a stored exchange rate.",
            )
        )

    # Step 4: Compare each line; an exact 2 percent price difference passes tolerance.
    conversion_factor = exchange_rate[1] if exchange_rate else Decimal(1)

    for index, line in enumerate(lines, 1):
        reference = line.item_code or line.description or str(index)
        qty, price = _d(line.qty), _d(line.unit_price)
        computed = qty * price if qty is not None and price is not None else None
        amount = _d(line.line_amount)
        if computed is not None and (amount is None or computed != amount):
            issues.append(
                _issue(
                    "LINE_TOTAL_VARIANCE",
                    "Line total does not equal quantity times unit price.",
                    amount,
                    computed,
                    amount - computed if amount is not None else None,
                    Decimal("0"),
                    reference,
                    "Correct the quantity, price, or line total.",
                )
            )
        if po is None or invoice_only:
            continue
        candidates = [
            pl
            for pl in po_lines
            if normalize_value(line.item_code)
            and normalize_value(pl.item_code) == normalize_value(line.item_code)
        ]
        if not candidates:
            issues.append(
                _issue(
                    "LINE_NOT_ON_PO",
                    "Invoice line has no matching PO SKU.",
                    reference,
                    [pl.item_code for pl in po_lines],
                    line=reference,
                    action=(
                        "Review the SKU; description similarity is only a suggestion."
                    ),
                )
            )
            continue
        pl = candidates[0]
        po_price = _d(pl.unit_price)
        if (
            qty is not None
            and (receipts or require_receipt)
            and received.get(pl.id, Decimal(0)) < qty
        ):
            issues.append(
                _issue(
                    "QUANTITY_VARIANCE",
                    "Invoice quantity exceeds accepted quantity received.",
                    qty,
                    received.get(pl.id, Decimal(0)),
                    qty - received.get(pl.id, Decimal(0)),
                    line=reference,
                    action="Verify the invoice quantity or receipt.",
                )
            )
        if price is None or po_price is None:
            issues.append(
                _issue(
                    "PRICE_VARIANCE",
                    "Invoice or PO unit price is missing.",
                    price,
                    po_price,
                    line=reference,
                    action="Verify the invoice and PO unit prices.",
                )
            )
        else:
            converted_price = price * conversion_factor
            delta = abs(converted_price - po_price)
            allowed = abs(po_price) * PRICE_TOLERANCE
            if delta > allowed:
                issues.append(
                    _issue(
                        "PRICE_VARIANCE",
                        "Invoice unit price is outside the allowed PO tolerance.",
                        converted_price,
                        po_price,
                        converted_price - po_price,
                        allowed,
                        reference,
                        "Review the invoice price or obtain PO approval.",
                    )
                )

    # Step 5: Reconcile tax, subtotal, header total, and duplicate invoice evidence.
    expected_tax = _d(po.tax_amount) if po else None
    actual_tax = _d(getattr(invoice, "tax_amount", None))
    if po and (
        expected_tax is None
        or actual_tax is None
        or abs(actual_tax * conversion_factor - expected_tax) > TAX_TOLERANCE
    ):
        issues.append(
            _issue(
                "TAX_VARIANCE",
                "Invoice tax differs from the PO tax beyond tolerance.",
                actual_tax * conversion_factor if actual_tax is not None else None,
                expected_tax,
                actual_tax * conversion_factor - expected_tax
                if actual_tax is not None and expected_tax is not None
                else None,
                TAX_TOLERANCE,
                action="Review tax amounts and tax treatment.",
            )
        )
    subtotal, total, tax = (
        _d(getattr(invoice, "subtotal", None)),
        _d(getattr(invoice, "total_amount", None)),
        _d(getattr(invoice, "tax_amount", None)),
    )
    expected_total = (
        subtotal + tax if subtotal is not None and tax is not None else None
    )
    line_amounts = [_d(line.line_amount) for line in lines]
    computed_subtotal = (
        sum(line_amounts, Decimal(0))
        if line_amounts and all(value is not None for value in line_amounts)
        else None
    )
    if (
        subtotal is not None
        and computed_subtotal is not None
        and abs(subtotal - computed_subtotal) > TAX_TOLERANCE
    ):
        issues.append(
            _issue(
                "HEADER_TOTAL_VARIANCE",
                "Invoice subtotal does not reconcile to its line totals.",
                subtotal,
                computed_subtotal,
                subtotal - computed_subtotal,
                TAX_TOLERANCE,
                action="Correct the invoice subtotal or line totals.",
            )
        )
    if (
        subtotal is None
        or tax is None
        or total is None
        or abs(total - (subtotal + tax)) > TAX_TOLERANCE
    ):
        issues.append(
            _issue(
                "HEADER_TOTAL_VARIANCE",
                "Invoice total does not reconcile to subtotal plus tax.",
                total,
                expected_total,
                total - expected_total
                if total is not None and expected_total is not None
                else None,
                TAX_TOLERANCE,
                action="Correct the invoice header totals.",
            )
        )
    if (
        invoice_vendor_id
        and getattr(invoice, "invoice_no", None)
        and find_duplicate_invoice(
            db,
            invoice_vendor_id,
            getattr(invoice, "invoice_no_normalized", None),
            exclude_invoice_id=invoice.id,
        )
    ):
        issues.append(
            _issue(
                "POSSIBLE_DUPLICATE",
                "Another invoice has this vendor and invoice number.",
                getattr(invoice, "invoice_no", None),
                "Unique vendor and invoice number",
                action="Check the existing invoice before proceeding.",
            )
        )

    # Step 6: Require at least 85 percent confidence for every material field.
    if invoice_only:
        information = _issue(
            "INVOICE_ONLY",
            "Invoice-only processing requires an approver decision.",
        )
        information["severity"] = "info"
        issues.append(information)
    elif not po_ref:
        information = _issue(
            "PO_CONFIRMATION_REQUIRED",
            "Select a suggested purchase order or choose to wait for a PO.",
        )
        information["severity"] = "info"
        issues.append(information)

    confidence = getattr(invoice, "field_confidence", None) or {}
    overall_percent = overall_confidence_percent(confidence)
    low_fields = []
    for field in MATERIAL_FIELDS:
        score = _score(confidence.get(field))
        if score is None or score < MIN_MATERIAL_CONFIDENCE:
            low_fields.append(field)
    for index, line in enumerate(lines, 1):
        fields = getattr(line, "field_confidence", None) or {}
        for field in ("qty", "unit_price", "line_amount"):
            score = _score(fields.get(field))
            if score is None or score < MIN_MATERIAL_CONFIDENCE:
                low_fields.append(f"line_{index}.{field}")
    if low_fields or Decimal(overall_percent) < MIN_MATERIAL_CONFIDENCE:
        issues.append(
            _issue(
                "LOW_CONFIDENCE",
                "One or more material fields are unverified or below the "
                "confidence threshold.",
                low_fields,
                f"At least {MIN_MATERIAL_CONFIDENCE}% confidence",
                action="Verify the listed fields.",
            )
        )
    confidence_levels = {}
    for field in MATERIAL_FIELDS:
        score = _score(confidence.get(field))
        confidence_levels[field] = (
            "low"
            if score is None
            else "high"
            if score >= 90
            else "medium"
            if score >= 75
            else "low"
        )
    for index, line in enumerate(lines, 1):
        fields = getattr(line, "field_confidence", None) or {}
        for field in ("qty", "unit_price", "line_amount"):
            score = _score(fields.get(field))
            confidence_levels[f"line_{index}.{field}"] = (
                "low"
                if score is None
                else "high"
                if score >= 90
                else "medium"
                if score >= 75
                else "low"
            )
    confidence_level = (
        "low"
        if any(value == "low" for value in confidence_levels.values())
        else "medium"
        if any(value == "medium" for value in confidence_levels.values())
        else "high"
    )
    source_document = (
        db.get(Document, invoice.source_document_id)
        if getattr(invoice, "source_document_id", None)
        else None
    )
    extracted = getattr(source_document, "extracted_json", None) or {}
    payment_terms = (
        extracted.get("payment_terms") if isinstance(extracted, dict) else None
    )
    comparison = build_comparison_snapshot(
        invoice,
        po,
        receipts,
        lines,
        po_lines,
        receipt_lines,
        issues,
        require_receipt=require_receipt,
        invoice_payment_terms=payment_terms,
    )
    blocking_issues = [item for item in issues if item.get("severity") != "info"]
    outcome = "ready_to_pay" if not blocking_issues else "needs_review"
    if invoice_only and not blocking_issues and invoice_only_requires_approval():
        outcome = "needs_approval"
    return {
        "rule_set_version": RULE_SET_VERSION,
        "outcome": outcome,
        "po_decision": (
            "invoice_only"
            if invoice_only
            else "waiting_for_po"
            if po_ref and not po_rows
            else "awaiting_confirmation"
            if not po_ref
            else None
        ),
        "confidence_level": confidence_level,
        "overall_confidence_percent": overall_percent,
        "field_confidence": confidence,
        "confidence_levels": confidence_levels,
        "low_confidence_fields": low_fields,
        "discrepancies": _json_safe(issues),
        "comparison": _json_safe(comparison),
        "inputs_summary": {
            "invoice_id": str(invoice.id),
            "po_number": po_number_raw,
            "vendor_id": str(invoice_vendor_id) if invoice_vendor_id else None,
            "invoice_number": getattr(invoice, "invoice_no", None),
            "currency": currency,
            "exchange_rate_id": str(exchange_rate[0].id) if exchange_rate else None,
            "exchange_rate": str(exchange_rate[1]) if exchange_rate else None,
            "invoice_total": str(getattr(invoice, "total_amount", None)),
            "invoice_subtotal": str(getattr(invoice, "subtotal", None)),
            "invoice_tax": str(getattr(invoice, "tax_amount", None)),
            "field_confidence": confidence,
            "po_ids": [str(row.id) for row in po_rows],
            "receipt_ids": [str(row.id) for row in receipts],
            "po_currency": po.currency if po else None,
            "po_tax": str(po.tax_amount) if po else None,
            "po_total": str(po.total_amount) if po else None,
            "invoice_lines": [
                {
                    "sku": line.item_code,
                    "quantity": str(line.qty),
                    "unit_price": str(line.unit_price),
                    "line_total": str(line.line_amount),
                    "field_confidence": getattr(line, "field_confidence", None) or {},
                }
                for line in lines
            ],
            "po_lines": [
                {
                    "sku": line.item_code,
                    "quantity": str(line.qty),
                    "unit_price": str(line.unit_price),
                }
                for line in po_lines
            ],
            "accepted_quantity_by_po_line": {
                str(line_id): str(quantity) for line_id, quantity in received.items()
            },
        },
    }
