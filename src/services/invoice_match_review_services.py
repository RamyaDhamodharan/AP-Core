"""Build display-ready invoice, purchase order, and receipt comparisons."""

from decimal import Decimal, InvalidOperation

from src.services.ap_decision_engine import PRICE_TOLERANCE, TAX_TOLERANCE
from src.services.po_matching_services import normalize_value


def _decimal(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _serialized(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _quantity_text(value):
    parsed = _decimal(value)
    return f"{parsed:,.3f}" if parsed is not None else str(value)


def _comparison(
    field,
    label,
    invoice_value,
    po_value,
    gr_value=None,
    *,
    tolerance=None,
    available=True,
    relation="PO",
):
    difference = None
    if not available or invoice_value is None or po_value is None:
        status = "not_available"
        message = (
            "Purchase order is not linked."
            if po_value is None and relation == "PO" and not available
            else f"{relation} value is not available for comparison."
        )
    else:
        invoice_number = _decimal(invoice_value)
        po_number = _decimal(po_value)
        if invoice_number is not None and po_number is not None:
            difference = invoice_number - po_number
            threshold = _decimal(tolerance) or Decimal(0)
            status = "match" if abs(difference) <= threshold else "mismatch"
            if status == "match":
                message = (
                    f"Invoice {label.lower()} matches {relation.lower()} "
                    f"{label.lower()}."
                )
            else:
                direction = "higher" if difference > 0 else "lower"
                message = (
                    f"Invoice {label.lower()} is {abs(difference):,.2f} {direction} "
                    f"than {relation.lower()} {label.lower()}."
                )
        else:
            status = (
                "match"
                if normalize_value(invoice_value) == normalize_value(po_value)
                else "mismatch"
            )
            message = (
                f"Invoice {label.lower()} matches {relation.lower()} {label.lower()}."
                if status == "match"
                else (
                    f"Invoice {label.lower()} does not match {relation.lower()} "
                    f"{label.lower()}."
                )
            )

    return {
        "field": field,
        "label": label,
        "invoice_value": _serialized(invoice_value),
        "po_value": _serialized(po_value),
        "gr_value": _serialized(gr_value),
        "difference": str(difference) if difference is not None else None,
        "tolerance": str(tolerance) if tolerance is not None else None,
        "status": status,
        "message": message,
    }


def build_invoice_match_review(invoice, invoice_lines, purchase_order, goods_receipts):
    """Return field-by-field evidence for the invoice detail match review table."""
    po_available = purchase_order is not None
    po_vendor = getattr(purchase_order, "vendor_name", None) if po_available else None
    invoice_vendor = getattr(invoice, "vendor_name_raw", None)
    gr_vendors = sorted(
        {
            receipt.vendor_name
            for receipt in goods_receipts
            if getattr(receipt, "vendor_name", None)
        }
    )
    rows = [
        _comparison(
            "total_amount",
            "Total Amount",
            invoice.total_amount,
            purchase_order.total_amount if po_available else None,
            tolerance=TAX_TOLERANCE,
            available=po_available,
        ),
        _comparison(
            "subtotal",
            "Subtotal",
            invoice.subtotal,
            purchase_order.subtotal if po_available else None,
            tolerance=TAX_TOLERANCE,
            available=po_available,
        ),
        _comparison(
            "tax_amount",
            "Tax",
            invoice.tax_amount,
            purchase_order.tax_amount if po_available else None,
            tolerance=TAX_TOLERANCE,
            available=po_available,
        ),
        _comparison(
            "invoice_date", "Invoice Date", invoice.invoice_date, None, available=False
        ),
        _comparison(
            "invoice_no", "Invoice Number", invoice.invoice_no, None, available=False
        ),
    ]
    vendor_matches = bool(
        po_available
        and (
            (
                getattr(invoice, "vendor_id", None)
                and invoice.vendor_id == purchase_order.vendor_id
            )
            or (
                invoice_vendor
                and po_vendor
                and normalize_value(invoice_vendor) == normalize_value(po_vendor)
            )
        )
    )
    vendor_row = _comparison(
        "vendor",
        "Vendor",
        invoice_vendor,
        po_vendor,
        ", ".join(gr_vendors) or None,
        available=po_available,
        relation="PO vendor",
    )
    if po_available and invoice_vendor is not None and po_vendor is not None:
        vendor_row["status"] = "match" if vendor_matches else "mismatch"
        vendor_row["message"] = (
            "Invoice vendor matches PO vendor."
            if vendor_matches
            else "Invoice vendor does not match PO vendor."
        )
    rows.append(vendor_row)

    if not invoice_lines:
        return rows

    po_lines = list(getattr(purchase_order, "lines", []) or []) if po_available else []
    receipt_lines = [
        line
        for receipt in goods_receipts
        for line in (getattr(receipt, "lines", []) or [])
    ]
    for index, invoice_line in enumerate(invoice_lines, start=1):
        item_key = normalize_value(invoice_line.item_code or invoice_line.description)
        po_line = next(
            (
                line
                for line in po_lines
                if (invoice_line.po_line_id and line.id == invoice_line.po_line_id)
                or (
                    item_key
                    and item_key
                    in {
                        normalize_value(line.item_code),
                        normalize_value(line.description),
                    }
                )
            ),
            None,
        )
        reference = invoice_line.item_code or invoice_line.description or str(index)
        line_label = f"Line {index} ({reference})"
        if not po_available:
            rows.extend(
                [
                    _comparison(
                        f"line_{index}_quantity",
                        f"{line_label} Quantity",
                        invoice_line.qty,
                        None,
                        available=False,
                    ),
                    _comparison(
                        f"line_{index}_unit_price",
                        f"{line_label} Unit Price",
                        invoice_line.unit_price,
                        None,
                        available=False,
                    ),
                ]
            )
            continue

        if po_line is None:
            for suffix, label, value in (
                ("quantity", "Quantity", invoice_line.qty),
                ("unit_price", "Unit Price", invoice_line.unit_price),
            ):
                rows.append(
                    {
                        "field": f"line_{index}_{suffix}",
                        "label": f"{line_label} {label}",
                        "invoice_value": _serialized(value),
                        "po_value": None,
                        "gr_value": None,
                        "difference": None,
                        "tolerance": None,
                        "status": "mismatch",
                        "message": f"{reference} is not present on the purchase order.",
                    }
                )
            continue

        matched_receipts = [
            line
            for line in receipt_lines
            if (line.po_line_id and line.po_line_id == po_line.id)
            or (
                item_key
                and item_key
                in {normalize_value(line.item_code), normalize_value(line.description)}
            )
        ]
        accepted = (
            sum(
                (
                    _decimal(line.accepted_qty) or Decimal(0)
                    for line in matched_receipts
                ),
                Decimal(0),
            )
            if goods_receipts
            else None
        )
        invoice_qty = _decimal(invoice_line.qty)
        qty_target = accepted
        qty_difference = (
            invoice_qty - qty_target
            if invoice_qty is not None and qty_target is not None
            else None
        )
        if qty_difference is None:
            qty_status = "not_available"
            qty_message = "Quantity evidence is not available for comparison."
        elif qty_difference <= 0:
            qty_status = "match"
            qty_target_name = "accepted receipt"
            qty_message = f"Invoice quantity is within the {qty_target_name} quantity."
        else:
            qty_status = "mismatch"
            qty_target_name = "accepted receipt"
            qty_message = (
                f"Invoice quantity is {_quantity_text(qty_difference)} higher than "
                f"the {qty_target_name} quantity."
            )
        rows.append(
            {
                "field": f"line_{index}_quantity",
                "label": f"{line_label} Quantity",
                "invoice_value": _serialized(invoice_line.qty),
                "po_value": _serialized(po_line.qty),
                "gr_value": _serialized(accepted),
                "difference": str(qty_difference)
                if qty_difference is not None
                else None,
                "tolerance": "0",
                "status": qty_status,
                "message": qty_message,
            }
        )
        price_tolerance = (
            abs(_decimal(po_line.unit_price) or Decimal(0)) * PRICE_TOLERANCE
        )
        rows.append(
            _comparison(
                f"line_{index}_unit_price",
                f"{line_label} Unit Price",
                invoice_line.unit_price,
                po_line.unit_price,
                tolerance=price_tolerance,
            )
        )
    return rows
