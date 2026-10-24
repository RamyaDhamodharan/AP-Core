"""Create the comparison snapshot stored with each invoice match run."""

from decimal import Decimal

from src.services.po_matching_services import normalize_po_reference, normalize_value
from src.services.vendor_resolution_services import normalize_vendor_tax_id


def _value(value):
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _line_value(line, name):
    return _value(getattr(line, name, None)) if line is not None else None


def _difference(first, second):
    if first is None or second is None:
        return None
    try:
        return str(Decimal(str(first)) - Decimal(str(second)))
    except Exception:
        return None


def _sum_known(rows, field):
    values = [
        Decimal(str(value))
        for row in rows
        if (value := getattr(row, field, None)) is not None
    ]
    return sum(values, Decimal(0)) if values else None


def saved_comparison_checks(comparison, match_outcome, require_receipt):
    """Project legacy checklist rows only from the saved match-run evidence."""
    headers = {
        row.get("field"): row
        for row in (comparison or {}).get("headers", [])
        if isinstance(row, dict)
    }
    lines = (comparison or {}).get("lines", [])
    issues = {
        item.get("code"): item
        for item in (match_outcome or {}).get("discrepancies", [])
        if isinstance(item, dict) and item.get("code")
    }

    def status_for(code, rows):
        if code == "MISSING_GR" and not require_receipt:
            return "not_required"
        if code in issues or any(row.get("status") == "mismatch" for row in rows):
            return "fail"
        if rows and all(row.get("status") == "match" for row in rows):
            return "pass"
        if rows and all(row.get("status") == "not_required" for row in rows):
            return "not_required"
        return "missing"

    definitions = (
        ("PO_NOT_RECEIVED", "Purchase order", ["po_number"]),
        ("VENDOR_MISMATCH", "Vendor", ["vendor_name", "vendor_tax_id"]),
        ("MISSING_GR", "Goods receipt", ["goods_receipt_count"]),
        ("LINE_NOT_ON_PO", "Invoice lines", None),
        ("QUANTITY_VARIANCE", "Received quantity", None),
        ("PRICE_VARIANCE", "Unit price", None),
        ("LINE_TOTAL_VARIANCE", "Line totals", None),
        ("TAX_VARIANCE", "Tax", ["tax_amount"]),
        ("HEADER_TOTAL_VARIANCE", "Header total", ["total_amount", "subtotal"]),
        ("CURRENCY_MISMATCH", "Currency", ["currency"]),
        ("POSSIBLE_DUPLICATE", "Duplicate invoice", []),
        ("LOW_CONFIDENCE", "Field confidence", []),
    )
    result = []
    for code, label, fields in definitions:
        rows = (
            [headers[field] for field in fields if field in headers]
            if fields is not None
            else [row for row in lines if row.get("invoice_line_id")]
        )
        if not fields and code in issues:
            rows = [{"status": "mismatch"}]
        status = status_for(code, rows)
        sample = rows[0] if rows else {}
        actual_count = sample.get("gr_value") if code == "MISSING_GR" else None
        result.append(
            {
                "code": code,
                "label": label,
                "status": status,
                "invoice_value": sample.get("invoice_value"),
                "expected_value": sample.get("po_value"),
                "actual_value": actual_count,
            }
        )
    return result


def _status(invoice_value, po_value, gr_value, reason_code=None, *, comparable=True):
    if reason_code:
        return "mismatch"
    if not comparable or (
        invoice_value is None and po_value is None and gr_value is None
    ):
        return "missing"
    if invoice_value is None or (po_value is None and gr_value is None):
        return "missing"
    return "match"


def build_comparison_snapshot(
    invoice,
    purchase_order,
    receipts,
    invoice_lines,
    po_lines,
    receipt_lines,
    issues,
    *,
    require_receipt,
    invoice_payment_terms=None,
):
    """Build a JSON-safe comparison payload from evidence loaded during matching."""
    po = purchase_order
    gr_names = sorted(
        {getattr(receipt, "gr_number", None) for receipt in receipts} - {None}
    )
    gr_dates = sorted(
        {_value(getattr(receipt, "receipt_date", None)) for receipt in receipts}
        - {None}
    )
    vendor = getattr(po, "vendor", None) if po is not None else None
    invoice_vendor = getattr(invoice, "vendor_name_raw", None)
    invoice_tax = getattr(invoice, "vendor_tax_id_raw", None)
    po_vendor_name = getattr(po, "vendor_name", None) if po is not None else None
    po_tax = getattr(vendor, "tax_id", None) if vendor is not None else None
    reason_codes = {issue.get("code") for issue in issues if issue.get("code")}
    issues_by_line = {}
    for issue in issues:
        reference = issue.get("line_reference")
        if reference:
            issues_by_line.setdefault(str(reference), set()).add(issue["code"])

    headers = []

    def add_header(
        field, label, invoice_value, po_value, gr_value, *, code=None, equal=None
    ):
        actual = (
            next((item for item in issues if item.get("code") == code), None)
            if code
            else None
        )
        comparable = equal is not None
        status = (
            "match"
            if comparable and equal
            else "mismatch"
            if (comparable and not equal) or (actual is not None and not comparable)
            else "missing"
        )
        headers.append(
            {
                "field": field,
                "label": label,
                "invoice_value": _value(invoice_value),
                "po_value": _value(po_value),
                "gr_value": _value(gr_value),
                "match": status == "match" if status in {"match", "mismatch"} else None,
                "status": status,
                "difference": _value(actual.get("difference")) if actual else None,
                "reason_code": actual.get("code")
                if actual and status == "mismatch"
                else code
                if code and status == "mismatch"
                else None,
                "next_action": actual.get("next_action")
                if actual
                else "Compare the invoice value with the purchase order."
                if code and status == "mismatch"
                else None,
            }
        )

    vendor_equal = bool(
        po
        and (
            (getattr(invoice, "vendor_id", None) and invoice.vendor_id == po.vendor_id)
            or (
                invoice_tax
                and po_tax
                and normalize_vendor_tax_id(invoice_tax)
                == normalize_vendor_tax_id(po_tax)
            )
        )
    )
    add_header(
        "vendor_name",
        "Vendor",
        invoice_vendor,
        po_vendor_name,
        ", ".join(gr_names) if gr_names else None,
        code="VENDOR_MISMATCH",
        equal=vendor_equal if po else None,
    )
    add_header(
        "vendor_tax_id",
        "Vendor Tax ID",
        invoice_tax,
        po_tax,
        None,
        code="VENDOR_MISMATCH",
        equal=(normalize_vendor_tax_id(invoice_tax) == normalize_vendor_tax_id(po_tax))
        if invoice_tax and po_tax
        else None,
    )
    invoice_po = getattr(invoice, "po_number_raw", None)
    po_number = getattr(po, "po_number", None) if po else None
    add_header(
        "po_number",
        "Purchase Order",
        invoice_po,
        po_number,
        gr_names or None,
        code="PO_NOT_RECEIVED",
        equal=(normalize_po_reference(invoice_po) == normalize_po_reference(po_number))
        if invoice_po and po_number
        else None,
    )
    currency = getattr(invoice, "currency", None)
    po_currency = getattr(po, "currency", None) if po else None
    add_header(
        "currency",
        "Currency",
        currency,
        po_currency,
        None,
        code="CURRENCY_MISMATCH",
        equal=(str(currency).upper() == str(po_currency).upper())
        if currency and po_currency
        else None,
    )
    add_header(
        "dates",
        "Invoice / PO / Goods Receipt Dates",
        getattr(invoice, "invoice_date", None),
        getattr(po, "po_date", None) if po else None,
        gr_dates or None,
    )
    add_header(
        "payment_terms",
        "Payment Terms",
        invoice_payment_terms,
        getattr(po, "payment_terms", None) if po else None,
        None,
    )
    for field, label, issue_code in (
        ("subtotal", "Subtotal", "HEADER_TOTAL_VARIANCE"),
        ("tax_amount", "Tax", "TAX_VARIANCE"),
        ("total_amount", "Total", "HEADER_TOTAL_VARIANCE"),
    ):
        invoice_value = getattr(invoice, field, None)
        po_value = getattr(po, field, None) if po else None
        difference = None
        if invoice_value is not None and po_value is not None:
            difference = _difference(invoice_value, po_value)
        add_header(
            field,
            label,
            invoice_value,
            po_value,
            None,
            code=issue_code,
            equal=(Decimal(difference) == 0) if difference is not None else None,
        )
        if difference is not None:
            headers[-1]["difference"] = difference

    if po is None and not require_receipt:
        gr_status = "not_required"
    elif not receipts and require_receipt:
        gr_status = "mismatch"
    elif not receipts:
        gr_status = "not_required"
    else:
        gr_status = "match"
    headers.append(
        {
            "field": "goods_receipt_count",
            "label": "Goods Receipts",
            "invoice_value": None,
            "po_value": None,
            "gr_value": len(receipts),
            "match": gr_status == "match" if gr_status != "not_required" else None,
            "status": gr_status,
            "difference": None,
            "reason_code": "MISSING_GR" if gr_status == "mismatch" else None,
            "next_action": "Record or link a goods receipt."
            if gr_status == "mismatch"
            else None,
        }
    )

    matched_po_ids = set()
    lines = []
    for index, invoice_line in enumerate(invoice_lines, start=1):
        sku = normalize_value(getattr(invoice_line, "item_code", None))
        po_line = next(
            (
                line
                for line in po_lines
                if sku and normalize_value(line.item_code) == sku
            ),
            None,
        )
        if po_line is not None:
            matched_po_ids.add(getattr(po_line, "id", None))
        matched_gr = [
            line
            for line in receipt_lines
            if po_line is not None
            and getattr(line, "po_line_id", None) == getattr(po_line, "id", None)
        ]
        line_reason_codes = set(
            issues_by_line.get(
                str(invoice_line.item_code or invoice_line.description or index), set()
            )
        )
        for issue in issues:
            if issue.get("code") in {"MISSING_GR", "CURRENCY_MISMATCH"}:
                line_reason_codes.add(issue["code"])
        priority = (
            "LINE_NOT_ON_PO",
            "MISSING_GR",
            "QUANTITY_VARIANCE",
            "PRICE_VARIANCE",
            "LINE_TOTAL_VARIANCE",
            "CURRENCY_MISMATCH",
        )
        reason_code = next(
            (code for code in priority if code in line_reason_codes), None
        )
        received = _sum_known(matched_gr, "received_qty")
        accepted = _sum_known(matched_gr, "accepted_qty")
        line_status = (
            "missing" if po_line is None else "mismatch" if reason_code else "match"
        )
        if not receipts and not require_receipt:
            line_status = "not_required" if po_line is not None else "missing"
        lines.append(
            {
                "invoice_line_id": str(getattr(invoice_line, "id", "")) or None,
                "po_line_id": str(getattr(po_line, "id", "")) or None,
                "goods_receipt_line_ids": [
                    str(getattr(line, "id", "")) for line in matched_gr
                ],
                "goods_receipt_lines": [
                    {
                        "id": str(getattr(line, "id", "")) or None,
                        "item_code": _line_value(line, "item_code"),
                        "description": _line_value(line, "description"),
                        "received_qty": _line_value(line, "received_qty"),
                        "accepted_qty": _line_value(line, "accepted_qty"),
                        "rejected_qty": _line_value(line, "rejected_qty"),
                    }
                    for line in matched_gr
                ],
                "item_code": {
                    "invoice": _line_value(invoice_line, "item_code"),
                    "po": _line_value(po_line, "item_code"),
                    "gr": _line_value(matched_gr[0], "item_code")
                    if matched_gr
                    else None,
                },
                "description": {
                    "invoice": _line_value(invoice_line, "description"),
                    "po": _line_value(po_line, "description"),
                    "gr": _line_value(matched_gr[0], "description")
                    if matched_gr
                    else None,
                },
                "quantity": {
                    "invoice": _line_value(invoice_line, "qty"),
                    "po_ordered": _line_value(po_line, "qty"),
                    "gr_received": str(received) if received is not None else None,
                    "gr_accepted": str(accepted) if accepted is not None else None,
                },
                "unit_price": {
                    "invoice": _line_value(invoice_line, "unit_price"),
                    "po": _line_value(po_line, "unit_price"),
                },
                "line_amount": {
                    "invoice": _line_value(invoice_line, "line_amount"),
                    "po": _line_value(po_line, "line_amount"),
                },
                "tax": {"invoice": None, "po": None},
                "difference": {
                    "quantity": _difference(
                        getattr(invoice_line, "qty", None), accepted
                    )
                    if getattr(invoice_line, "qty", None) is not None
                    and accepted is not None
                    else None,
                    "unit_price": _difference(
                        getattr(invoice_line, "unit_price", None),
                        getattr(po_line, "unit_price", None),
                    )
                    if po_line is not None
                    and getattr(invoice_line, "unit_price", None) is not None
                    and po_line.unit_price is not None
                    else None,
                    "line_amount": _difference(
                        getattr(invoice_line, "line_amount", None),
                        getattr(po_line, "line_amount", None),
                    )
                    if po_line is not None
                    and getattr(invoice_line, "line_amount", None) is not None
                    and po_line.line_amount is not None
                    else None,
                },
                "status": line_status,
                "match": line_status == "match"
                if line_status in {"match", "mismatch"}
                else None,
                "reason_code": reason_code
                or ("LINE_NOT_ON_PO" if po_line is None else None),
                "reason_codes": sorted(line_reason_codes),
                "next_action": next(
                    (
                        issue.get("next_action")
                        for issue in issues
                        if issue.get("code") == reason_code
                    ),
                    "Match or verify this invoice line." if po_line is None else None,
                ),
            }
        )
    for po_line in po_lines:
        if getattr(po_line, "id", None) in matched_po_ids:
            continue
        po_receipt_lines = [
            line
            for line in receipt_lines
            if getattr(line, "po_line_id", None) == getattr(po_line, "id", None)
        ]
        po_received = _sum_known(po_receipt_lines, "received_qty")
        po_accepted = _sum_known(po_receipt_lines, "accepted_qty")
        lines.append(
            {
                "invoice_line_id": None,
                "po_line_id": str(getattr(po_line, "id", "")) or None,
                "goods_receipt_line_ids": [
                    str(getattr(line, "id", "")) for line in po_receipt_lines
                ],
                "goods_receipt_lines": [
                    {
                        "id": str(getattr(line, "id", "")) or None,
                        "item_code": _line_value(line, "item_code"),
                        "description": _line_value(line, "description"),
                        "received_qty": _line_value(line, "received_qty"),
                        "accepted_qty": _line_value(line, "accepted_qty"),
                        "rejected_qty": _line_value(line, "rejected_qty"),
                    }
                    for line in po_receipt_lines
                ],
                "item_code": {
                    "invoice": None,
                    "po": _line_value(po_line, "item_code"),
                    "gr": _line_value(po_receipt_lines[0], "item_code")
                    if po_receipt_lines
                    else None,
                },
                "description": {
                    "invoice": None,
                    "po": _line_value(po_line, "description"),
                    "gr": _line_value(po_receipt_lines[0], "description")
                    if po_receipt_lines
                    else None,
                },
                "quantity": {
                    "invoice": None,
                    "po_ordered": _line_value(po_line, "qty"),
                    "gr_received": str(po_received)
                    if po_received is not None
                    else None,
                    "gr_accepted": str(po_accepted)
                    if po_accepted is not None
                    else None,
                },
                "unit_price": {
                    "invoice": None,
                    "po": _line_value(po_line, "unit_price"),
                },
                "line_amount": {
                    "invoice": None,
                    "po": _line_value(po_line, "line_amount"),
                },
                "tax": {"invoice": None, "po": None},
                "difference": {
                    "quantity": None,
                    "unit_price": None,
                    "line_amount": None,
                },
                "status": "missing",
                "match": None,
                "reason_code": "UNMATCHED_PO_LINE",
                "reason_codes": ["UNMATCHED_PO_LINE"],
                "next_action": "Confirm whether this PO line belongs on the invoice.",
            }
        )
    return {"headers": headers, "lines": lines, "reason_codes": sorted(reason_codes)}
