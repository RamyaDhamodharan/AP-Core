import re
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session, selectinload

from src.models.document_models import Document
from src.models.goods_receipt_models import GoodsReceipt
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.services.invoice_number_services import normalize_invoice_no


def normalize_value(value: object) -> str:
    """Normalize text for case-insensitive comparison."""
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def normalize_amount(value: object) -> Decimal | None:
    """Convert a monetary value to Decimal safely."""
    if value is None or isinstance(value, bool):
        return None

    try:
        cleaned = re.sub(r"[,$\s]", "", str(value))
        amount = Decimal(cleaned)

        if not amount.is_finite():
            return None

        return amount
    except (InvalidOperation, ValueError):
        return None


def match_invoice_to_po(
    db: Session,
    invoice_data: dict,
    invoice_document_id,
) -> dict:
    """
    Match an invoice against an extracted purchase order.

    This initial version requires an exact vendor, PO number,
    and total match. It does not support partial invoices.
    """
    vendor = normalize_value(invoice_data.get("vendor"))
    order_number = normalize_value(invoice_data.get("order_number"))
    invoice_total = normalize_amount(invoice_data.get("total"))

    if not vendor:
        return {
            "matched": False,
            "reason": "Invoice vendor is missing",
            "po_document_id": None,
        }

    if not order_number:
        return {
            "matched": False,
            "reason": "Invoice PO/order number is missing",
            "po_document_id": None,
        }

    if invoice_total is None:
        return {
            "matched": False,
            "reason": "Invoice total is missing or invalid",
            "po_document_id": None,
        }

    purchase_orders = (
        db.query(Document)
        .filter(
            Document.doc_type == "po",
            Document.extracted_json.isnot(None),
        )
        .all()
    )

    for po in purchase_orders:
        if po.id == invoice_document_id:
            continue

        po_data = po.extracted_json or {}

        if not isinstance(po_data, dict):
            continue

        po_vendor = normalize_value(po_data.get("vendor"))
        po_number = normalize_value(po_data.get("order_number"))
        po_total = normalize_amount(po_data.get("total"))

        if po_number != order_number:
            continue

        if po_vendor != vendor:
            return {
                "matched": False,
                "reason": "PO vendor does not match invoice vendor",
                "po_document_id": str(po.id),
            }

        if po_total is None:
            return {
                "matched": False,
                "reason": "PO total is missing or invalid",
                "po_document_id": str(po.id),
            }

        if invoice_total != po_total:
            return {
                "matched": False,
                "reason": "Invoice total does not match PO total",
                "po_document_id": str(po.id),
            }

        return {
            "matched": True,
            "reason": "Vendor, PO number, and total match",
            "po_document_id": str(po.id),
        }

    return {
        "matched": False,
        "reason": "Matching purchase order not found",
        "po_document_id": None,
    }


def match_invoice_three_way(db: Session, invoice: Invoice) -> dict:
    """Compare a persisted invoice with its current PO and posted receipts.

    This is advisory only: every result is returned for human review and this
    function never changes invoice approval state.
    """
    issues: list[str] = []
    invoice_no = getattr(invoice, "invoice_no_normalized", None) or (
        normalize_invoice_no(invoice.invoice_no)
    )
    if not invoice_no or not invoice.vendor_id:
        issues.append("Invoice number or resolved vendor is missing; review required")
    else:
        duplicate = (
            db.query(Invoice.id)
            .filter(
                Invoice.id != invoice.id,
                Invoice.is_current.is_(True),
                Invoice.vendor_id == invoice.vendor_id,
                Invoice.invoice_no_normalized == invoice_no,
            )
            .first()
        )
        if duplicate:
            issues.append("Duplicate invoice detected for this vendor")

    if not invoice.po_id:
        issues.append("Purchase order is missing or unresolved")
        return {
            "matched": False,
            "requires_review": True,
            "issues": issues,
            "po_id": None,
            "goods_receipt_ids": [],
        }

    po = db.get(PurchaseOrder, invoice.po_id)
    if po is None:
        issues.append("Referenced purchase order was not found")
        return {
            "matched": False,
            "requires_review": True,
            "issues": issues,
            "po_id": str(invoice.po_id),
            "goods_receipt_ids": [],
        }
    if (
        invoice.vendor_id is None
        or po.vendor_id is None
        or invoice.vendor_id != po.vendor_id
    ):
        issues.append("Invoice vendor does not match purchase order vendor")
    if invoice.po_number_raw and normalize_value(
        invoice.po_number_raw
    ) != normalize_value(po.po_number):
        issues.append("Invoice PO number does not match referenced purchase order")
    if (
        invoice.currency is None
        or po.currency is None
        or invoice.currency.strip().upper() != po.currency.strip().upper()
    ):
        issues.append("Invoice or PO currency is missing or does not match")
    if invoice.total_amount is None or po.total_amount is None:
        issues.append("Invoice or PO total is missing; review required")
    elif invoice.total_amount > po.total_amount:
        issues.append("Invoice total exceeds PO total")

    receipts = (
        db.query(GoodsReceipt)
        .options(selectinload(GoodsReceipt.lines))
        .filter(GoodsReceipt.po_id == po.id)
        .all()
    )
    if not receipts:
        issues.append("No goods receipt is recorded for this purchase order")
        return {
            "matched": False,
            "requires_review": True,
            "issues": issues,
            "po_id": str(po.id),
            "goods_receipt_ids": [],
        }
    if any(r.vendor_id != po.vendor_id for r in receipts):
        issues.append("Goods receipt vendor does not match purchase order vendor")

    po_lines = (
        db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all()
    )
    invoice_lines = (
        db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
    )
    if not po_lines or not invoice_lines:
        issues.append("PO or invoice line quantities are missing; review required")
    else:
        po_by_id = {line.id: line for line in po_lines}
        accepted: dict[object, Decimal] = {}
        received: dict[object, Decimal] = {}
        for receipt in receipts:
            for line in receipt.lines:
                accepted[line.po_line_id] = (
                    accepted.get(line.po_line_id, Decimal("0")) + line.accepted_qty
                )
                received[line.po_line_id] = (
                    received.get(line.po_line_id, Decimal("0")) + line.received_qty
                )
        for po_line in po_lines:
            po_qty = Decimal(str(po_line.qty or 0))
            if received.get(po_line.id, Decimal("0")) > po_qty:
                issues.append(
                    f"Over-receipt for PO line {po_line.item_code or po_line.id}"
                )
            if accepted.get(po_line.id, Decimal("0")) < po_qty:
                issues.append(
                    f"Partial receipt for PO line {po_line.item_code or po_line.id}"
                )
        for line in invoice_lines:
            line_po_id = line.po_line_id
            if line_po_id is None:
                candidates = [
                    p
                    for p in po_lines
                    if (
                        line.item_code
                        and normalize_value(p.item_code)
                        == normalize_value(line.item_code)
                    )
                    or (
                        not line.item_code
                        and line.description
                        and normalize_value(p.description)
                        == normalize_value(line.description)
                    )
                ]
                if len(candidates) != 1:
                    issues.append(
                        "Invoice line cannot be uniquely matched to a PO line; "
                        "review required"
                    )
                    continue
                line_po_id = candidates[0].id
            po_line = po_by_id.get(line_po_id)
            if po_line is None:
                issues.append(
                    "Invoice line references a PO line outside this purchase order"
                )
                continue
            qty = Decimal(str(line.qty or 0))
            if qty <= 0:
                issues.append(
                    "Invoice line quantity is missing or invalid; review required"
                )
            elif qty > accepted.get(line_po_id, Decimal("0")):
                issues.append(
                    "Invoice quantity exceeds accepted receipt quantity for PO "
                    f"line {po_line.item_code or po_line.id}"
                )
            if (
                qty > 0
                and line.line_amount is not None
                and po_line.unit_price is not None
            ):
                expected_amount = Decimal(str(po_line.unit_price)) * qty
                if abs(Decimal(str(line.line_amount)) - expected_amount) > Decimal(
                    "0.01"
                ):
                    issues.append(
                        "Invoice line amount does not match PO unit price for "
                        f"PO line {po_line.item_code or po_line.id}"
                    )
        if (
            all(
                accepted.get(p.id, Decimal("0")) >= Decimal(str(p.qty or 0))
                for p in po_lines
            )
            and invoice.total_amount is not None
            and po.total_amount is not None
            and abs(invoice.total_amount - po.total_amount) > Decimal("0.01")
        ):
            issues.append("Invoice total does not match fully received PO total")

    return {
        "matched": not issues,
        "requires_review": True,
        "issues": issues,
        "po_id": str(po.id),
        "goods_receipt_ids": [str(r.id) for r in receipts],
    }
