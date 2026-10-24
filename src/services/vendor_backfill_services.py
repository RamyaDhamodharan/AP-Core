"""Idempotently repair vendor links on saved AP records."""

import uuid

from sqlalchemy.orm import Session

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.goods_receipt_models import GoodsReceipt
from src.models.invoice_models import Invoice
from src.models.purchase_order_models import PurchaseOrder
from src.services.vendor_resolution_services import resolve_vendor


def _record_vendor(db: Session, document: Document | None, name=None, tax_id=None):
    extracted = getattr(document, "extracted_json", None) or {}
    return resolve_vendor(
        db,
        name or extracted.get("vendor") or extracted.get("vendor_name"),
        tax_id=(
            tax_id
            or extracted.get("vendor_tax_id")
            or extracted.get("tax_id")
        ),
    )


def backfill_vendor_links(db: Session, actor_id: uuid.UUID | None = None) -> dict:
    """Link exactly resolvable null-vendor records and rematch changed invoices."""
    counts = {
        "documents_scanned": 0,
        "documents_linked": 0,
        "invoices_scanned": 0,
        "invoices_linked": 0,
        "purchase_orders_scanned": 0,
        "purchase_orders_linked": 0,
        "goods_receipts_scanned": 0,
        "goods_receipts_linked": 0,
        "invoices_rematched": 0,
    }
    documents = (
        db.query(Document)
        .filter(Document.extracted_json.is_not(None))
        .order_by(Document.created_at.asc(), Document.id.asc())
        .with_for_update(skip_locked=True)
        .all()
    )
    document_by_id = {row.id: row for row in documents}
    for document in documents:
        counts["documents_scanned"] += 1
        if document.vendor_id is not None:
            continue
        vendor = _record_vendor(db, document)
        if vendor is not None:
            document.vendor_id = vendor.id
            counts["documents_linked"] += 1

    rematch_ids = set()
    invoices = (
        db.query(Invoice)
        .filter(Invoice.vendor_id.is_(None))
        .order_by(Invoice.created_at.asc(), Invoice.id.asc())
        .with_for_update(skip_locked=True)
        .all()
    )
    for invoice in invoices:
        counts["invoices_scanned"] += 1
        document = document_by_id.get(invoice.source_document_id)
        vendor = _record_vendor(
            db,
            document,
            getattr(invoice, "vendor_name_raw", None),
            getattr(invoice, "vendor_tax_id_raw", None),
        )
        if vendor is None:
            continue
        invoice.vendor_id = vendor.id
        invoice.updated_by = actor_id
        if document is not None and document.vendor_id is None:
            document.vendor_id = vendor.id
            counts["documents_linked"] += 1
        counts["invoices_linked"] += 1
        rematch_ids.add(invoice.id)

    for model, key in (
        (PurchaseOrder, "purchase_orders"),
        (GoodsReceipt, "goods_receipts"),
    ):
        rows = (
            db.query(model)
            .filter(model.vendor_id.is_(None))
            .order_by(model.created_at.asc(), model.id.asc())
            .with_for_update(skip_locked=True)
            .all()
        )
        for row in rows:
            counts[f"{key}_scanned"] += 1
            document = document_by_id.get(row.source_document_id)
            vendor = _record_vendor(db, document)
            if vendor is None:
                continue
            row.vendor_id = vendor.id
            row.updated_by = actor_id
            counts[f"{key}_linked"] += 1
            if document is not None and document.vendor_id is None:
                document.vendor_id = vendor.id
                counts["documents_linked"] += 1

    if rematch_ids:
        from src.services.invoice_matching_services import run_match_for_invoice

        for invoice_id in sorted(rematch_ids, key=str):
            run_match_for_invoice(db, invoice_id, actor_id, force=True)
            counts["invoices_rematched"] += 1
            invoice = db.get(Invoice, invoice_id)
            if invoice is None or invoice.po_id is None:
                continue
            purchase_order = db.get(PurchaseOrder, invoice.po_id)
            if purchase_order is None:
                continue
            if purchase_order.vendor_id is None:
                purchase_order.vendor_id = invoice.vendor_id
                purchase_order.updated_by = actor_id
                counts["purchase_orders_linked"] += 1
            receipts = (
                db.query(GoodsReceipt)
                .filter(GoodsReceipt.po_id == purchase_order.id)
                .all()
            )
            for receipt in receipts:
                if receipt.vendor_id is None:
                    receipt.vendor_id = invoice.vendor_id
                    receipt.updated_by = actor_id
                    counts["goods_receipts_linked"] += 1

    db.add(
        AuditLog(
            action="vendor_link_backfill_completed",
            actor=str(actor_id) if actor_id else "system",
            entity_type="vendor",
            result="success",
            output_summary=counts,
            message=(
                "Null vendor links were backfilled where exact resolution succeeded"
            ),
            created_by=actor_id,
        )
    )
    return counts
