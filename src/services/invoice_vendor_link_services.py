"""Resolve and persist a missing vendor link before invoice matching."""

from sqlalchemy.orm import Session

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.invoice_models import Invoice
from src.models.vendor_models import Vendor
from src.services.vendor_resolution_services import resolve_vendor


def link_invoice_vendor_if_resolvable(
    db: Session, invoice: Invoice, *, actor_id=None
) -> Vendor | None:
    """Link an unresolved invoice and source document when exact evidence resolves."""
    invoice_vendor_id = getattr(invoice, "vendor_id", None)
    if invoice_vendor_id:
        return db.get(Vendor, invoice_vendor_id)
    source_document_id = getattr(invoice, "source_document_id", None)
    document = db.get(Document, source_document_id) if source_document_id else None
    extracted = getattr(document, "extracted_json", None) or {}
    vendor = resolve_vendor(
        db,
        getattr(invoice, "vendor_name_raw", None)
        or extracted.get("vendor")
        or extracted.get("vendor_name"),
        tax_id=(
            getattr(invoice, "vendor_tax_id_raw", None)
            or extracted.get("vendor_tax_id")
            or extracted.get("tax_id")
        ),
    )
    if vendor is None:
        return None
    invoice.vendor_id = vendor.id
    invoice.updated_by = actor_id
    if document is not None and document.vendor_id is None:
        document.vendor_id = vendor.id
    db.add(
        AuditLog(
            action="vendor_linked_automatically",
            actor="system",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value={"vendor_id": None},
            new_value={"vendor_id": str(vendor.id), "vendor_name": vendor.name},
            result="success",
            message="Vendor linked automatically before invoice matching",
            created_by=actor_id,
        )
    )
    return vendor
