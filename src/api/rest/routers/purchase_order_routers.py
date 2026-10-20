import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.purchase_order_models import PurchaseOrder
from src.schemas.purchase_order_schemas import (
    PurchaseOrderListResponse,
    PurchaseOrderResponse,
)

router = APIRouter(
    prefix="/api/purchase-orders",
    tags=["Purchase Orders"],
    dependencies=[Depends(get_current_user)],
)


def _query():
    return select(PurchaseOrder).options(
        selectinload(PurchaseOrder.lines), selectinload(PurchaseOrder.vendor)
    )


@router.get("", response_model=PurchaseOrderListResponse)
def list_purchase_orders(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    vendor_id: uuid.UUID | None = None,
    source_document_id: uuid.UUID | None = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    current_only: bool = True,
    db: Session = Depends(get_db),
) -> PurchaseOrderListResponse:
    filters = []
    if current_only:
        filters.append(PurchaseOrder.is_current.is_(True))
    if vendor_id is not None:
        filters.append(PurchaseOrder.vendor_id == vendor_id)
    if source_document_id is not None:
        filters.append(PurchaseOrder.source_document_id == source_document_id)
    if search:
        filters.append(PurchaseOrder.po_number.ilike(f"%{search.strip()}%"))

    total = db.scalar(select(func.count(PurchaseOrder.id)).where(*filters)) or 0
    orders = db.scalars(
        _query()
        .where(*filters)
        .order_by(PurchaseOrder.po_date.desc(), PurchaseOrder.po_number)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return PurchaseOrderListResponse(
        items=orders, total=total, page=page, page_size=page_size
    )


@router.get("/{po_id}", response_model=PurchaseOrderResponse)
def get_purchase_order(po_id: uuid.UUID, db: Session = Depends(get_db)):
    order = db.scalar(_query().where(PurchaseOrder.id == po_id))
    if order is None:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return order
