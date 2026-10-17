
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from src.models.document_models import Document


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
