"""
Application module for purchase order routers.
Flow: purchase-order review and confirmation.
Called by: the framework, CLI, or application entry point.
Calls: audit_models, dependencies, document_models, procurement_backfill_services,
purchase_order_models.
Tables: audit_log, documents, purchase_orders, vendors.
"""
import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select, true
from sqlalchemy.orm import Session, selectinload

from src.api.rest.dependencies import get_current_user, require_role
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.invoice_models import Invoice
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.models.vendor_models import Vendor
from src.schemas.purchase_order_schemas import (
    PurchaseOrderAmendmentRequest,
    PurchaseOrderListResponse,
    PurchaseOrderResponse,
    PurchaseOrderVendorReassignmentRequest,
)
from src.services.invoice_matching_services import run_match_for_invoice
from src.services.po_matching_services import normalize_value

router = APIRouter(
    prefix="/api/purchase-orders",
    tags=["Purchase Orders"],
    dependencies=[Depends(get_current_user)],
)


def _query():
    return select(PurchaseOrder).options(
        selectinload(PurchaseOrder.lines), selectinload(PurchaseOrder.vendor)
    )


def _owner_filter(owner_id: uuid.UUID, current_user: dict | None = None):
    if current_user and current_user.get("role") == "admin":
        return true()
    return or_(
        PurchaseOrder.created_by == owner_id,
        PurchaseOrder.source_document_id.in_(
            select(Document.id).where(
                or_(
                    Document.created_by == owner_id,
                    Document.source.in_(["email", "email_poll"]),
                )
            )
        ),
    )


@router.get("", response_model=PurchaseOrderListResponse)
def list_purchase_orders(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    vendor_id: uuid.UUID | None = None,
    source_document_id: uuid.UUID | None = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    current_only: bool = False,
    # Retained for compatibility with older clients; PO confidence is not a filter.
    status: Literal["all", "needs_verification", "verified"] = "all",
    date_from: date | None = None,
    date_to: date | None = None,
    sort_by: Annotated[
        str, Query(pattern="^(po_date|po_number|created_at)$")
    ] = "po_date",
    sort_dir: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> PurchaseOrderListResponse:
    """
    List list purchase orders.
    Inputs: page (Annotated[int, Query(ge=1)]), page_size (Annotated[int, Query(ge=1,
        le=100)]), vendor_id (uuid.UUID | None), source_document_id (uuid.UUID | None),
        search (Annotated[str | None, Query(max_length=200)]), current_only (bool),
        status (Literal["all", "needs_verification", "verified"]), date_from (date |
        None), date_to (date | None), ….
    Output: PurchaseOrderListResponse.
    Effects: reads database rows.
    Results: no workflow status or reason code directly.
    """
    try:
        owner_id = uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc
    try:
        from src.services.procurement_backfill_services import (
            backfill_procurement_documents,
        )

        backfill_procurement_documents(db, owner_id)
    except Exception:
        db.rollback()
    filters = [_owner_filter(owner_id, current_user)]
    if current_only:
        filters.append(PurchaseOrder.is_current.is_(True))
    if vendor_id is not None:
        filters.append(PurchaseOrder.vendor_id == vendor_id)
    if source_document_id is not None:
        filters.append(PurchaseOrder.source_document_id == source_document_id)
    if search:
        filters.append(PurchaseOrder.po_number.ilike(f"%{search.strip()}%"))
    if date_from:
        filters.append(PurchaseOrder.po_date >= date_from)
    if date_to:
        filters.append(PurchaseOrder.po_date <= date_to)
    total = db.scalar(select(func.count(PurchaseOrder.id)).where(*filters)) or 0
    count_filters = [_owner_filter(owner_id, current_user)]
    if current_only:
        count_filters.append(PurchaseOrder.is_current.is_(True))
    if vendor_id is not None:
        count_filters.append(PurchaseOrder.vendor_id == vendor_id)
    if source_document_id is not None:
        count_filters.append(PurchaseOrder.source_document_id == source_document_id)
    if search:
        count_filters.append(PurchaseOrder.po_number.ilike(f"%{search.strip()}%"))
    if date_from:
        count_filters.append(PurchaseOrder.po_date >= date_from)
    if date_to:
        count_filters.append(PurchaseOrder.po_date <= date_to)
    counts = {
        "all": db.scalar(select(func.count(PurchaseOrder.id)).where(*count_filters))
        or 0
    }
    sort_column = getattr(PurchaseOrder, sort_by)
    sort_order = sort_column.asc() if sort_dir == "asc" else sort_column.desc()
    orders = db.scalars(
        _query()
        .where(*filters)
        .order_by(sort_order, PurchaseOrder.po_number)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return PurchaseOrderListResponse(
        items=orders, total=total, page=page, page_size=page_size, counts=counts
    )


@router.get("/{po_id}", response_model=PurchaseOrderResponse)
def get_purchase_order(
    po_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Return get purchase order.
    Inputs: po_id (uuid.UUID), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows.
    Results: no workflow status or reason code directly.
    """
    try:
        owner_id = uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc
    order = db.scalar(
        _query().where(PurchaseOrder.id == po_id, _owner_filter(owner_id, current_user))
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return order


@router.patch(
    "/{po_id}/vendor",
    response_model=PurchaseOrderResponse,
    dependencies=[Depends(require_role("admin"))],
)
def reassign_purchase_order_vendor(
    po_id: uuid.UUID,
    payload: PurchaseOrderVendorReassignmentRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Reassign reassign purchase order vendor.
    Inputs: po_id (uuid.UUID), payload (PurchaseOrderVendorReassignmentRequest), db
        (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries.
    Results: no workflow status or reason code directly.
    """
    try:
        owner_id = uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc
    order = db.scalar(
        _query()
        .where(PurchaseOrder.id == po_id, _owner_filter(owner_id, current_user))
        .with_for_update()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    vendor = db.get(Vendor, payload.vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if not vendor.is_active:
        raise HTTPException(status_code=409, detail="Vendor is inactive")
    old_vendor_id = order.vendor_id
    order.vendor_id = vendor.id
    db.add(
        AuditLog(
            action="purchase_order_vendor_reassigned",
            entity_type="purchase_order",
            entity_id=order.id,
            old_value={"vendor_id": str(old_vendor_id) if old_vendor_id else None},
            new_value={"vendor_id": str(vendor.id), "vendor_name": vendor.name},
            created_by=owner_id,
            updated_by=owner_id,
        )
    )
    db.commit()
    return db.scalar(
        _query().where(
            PurchaseOrder.id == order.id,
            _owner_filter(owner_id, current_user),
        )
    )


@router.post(
    "/{po_id}/amendments",
    response_model=PurchaseOrderResponse,
    status_code=201,
    dependencies=[Depends(require_role("admin"))],
)
def amend_purchase_order(
    po_id: uuid.UUID,
    payload: PurchaseOrderAmendmentRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Create an audited PO version and automatically re-match its invoices."""
    try:
        actor_id = uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc
    order = db.scalar(
        _query()
        .where(PurchaseOrder.id == po_id, _owner_filter(actor_id, current_user))
        .with_for_update()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    if not order.is_current:
        raise HTTPException(
            status_code=409, detail="Only the current purchase order can be amended"
        )

    old_snapshot = {
        "version_no": order.version_no,
        "vendor_id": str(order.vendor_id) if order.vendor_id else None,
        "po_date": order.po_date.isoformat() if order.po_date else None,
        "currency": order.currency,
        "subtotal": str(order.subtotal) if order.subtotal is not None else None,
        "tax_amount": str(order.tax_amount) if order.tax_amount is not None else None,
        "total_amount": str(order.total_amount)
        if order.total_amount is not None
        else None,
        "lines": [
            {
                "item_code": line.item_code,
                "qty": str(line.qty) if line.qty is not None else None,
                "unit_price": str(line.unit_price)
                if line.unit_price is not None
                else None,
                "line_amount": str(line.line_amount)
                if line.line_amount is not None
                else None,
            }
            for line in order.lines
        ],
    }
    previous_lines = list(order.lines)
    order.is_current = False
    db.flush()
    amended = PurchaseOrder(
        po_number=order.po_number,
        version_no=order.version_no + 1,
        is_current=True,
        vendor_id=order.vendor_id,
        source_document_id=None,
        po_date=payload.po_date or order.po_date,
        currency=payload.currency or order.currency,
        subtotal=payload.subtotal if payload.subtotal is not None else order.subtotal,
        tax_amount=(
            payload.tax_amount if payload.tax_amount is not None else order.tax_amount
        ),
        tax_rate=payload.tax_rate if payload.tax_rate is not None else order.tax_rate,
        total_amount=(
            payload.total_amount
            if payload.total_amount is not None
            else order.total_amount
        ),
        payment_terms=payload.payment_terms or order.payment_terms,
        delivery_details=payload.delivery_details or order.delivery_details,
        field_confidence=order.field_confidence,
        needs_verification=order.needs_verification,
        created_by=actor_id,
        updated_by=actor_id,
    )
    db.add(amended)
    db.flush()
    if payload.lines is not None:
        amended.lines = [
            PurchaseOrderLine(**line.model_dump(), created_by=actor_id)
            for line in payload.lines
        ]
    else:
        amended.lines = [
            PurchaseOrderLine(
                item_code=line.item_code,
                description=line.description,
                uom=line.uom,
                qty=line.qty,
                unit_price=line.unit_price,
                line_amount=line.line_amount,
                field_confidence=line.field_confidence,
                created_by=actor_id,
            )
            for line in previous_lines
        ]
    db.flush()
    new_snapshot = {
        **old_snapshot,
        "version_no": amended.version_no,
        "currency": amended.currency,
        "subtotal": str(amended.subtotal) if amended.subtotal is not None else None,
        "tax_amount": str(amended.tax_amount)
        if amended.tax_amount is not None
        else None,
        "total_amount": str(amended.total_amount)
        if amended.total_amount is not None
        else None,
        "lines": [
            {
                "item_code": line.item_code,
                "qty": str(line.qty) if line.qty is not None else None,
                "unit_price": str(line.unit_price)
                if line.unit_price is not None
                else None,
                "line_amount": str(line.line_amount)
                if line.line_amount is not None
                else None,
            }
            for line in amended.lines
        ],
    }
    db.add(
        AuditLog(
            action="purchase_order_amended",
            actor=str(actor_id),
            entity_type="purchase_order",
            entity_id=amended.id,
            old_value=old_snapshot,
            new_value=new_snapshot,
            result="success",
            message=f"PO {order.po_number} amended to version {amended.version_no}",
            reason=payload.reason,
            created_by=actor_id,
            updated_by=actor_id,
        )
    )
    invoices = (
        db.query(Invoice).filter(Invoice.is_current.is_(True)).with_for_update().all()
    )
    for invoice in invoices:
        if normalize_value(invoice.po_number_raw) != normalize_value(order.po_number):
            continue
        run_match_for_invoice(
            db,
            invoice.id,
            force=True,
            idempotent=True,
            trigger="po_amended",
        )
    db.commit()
    result = db.scalar(
        _query().where(
            PurchaseOrder.id == amended.id,
            _owner_filter(actor_id, current_user),
        )
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Amended PO not found")
    return result
