import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.models.vendor_models import Vendor
from src.schemas.goods_receipt_schemas import (
    GoodsReceiptCreate,
    GoodsReceiptResponse,
    GoodsReceiptUpdate,
)

router = APIRouter(prefix="/api/goods-receipts", tags=["Goods Receipts"])
_GR_NUMBER_CONSTRAINTS = {
    "uq_goods_receipts_gr_number",
    "uq_goods_receipts_gr_number_norm",
}


def _is_goods_receipt_number_conflict(exc: IntegrityError) -> bool:
    diagnostic = getattr(exc.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None) in _GR_NUMBER_CONSTRAINTS


def _handle_goods_receipt_integrity_error(
    db: Session, exc: IntegrityError
) -> None:
    db.rollback()
    if _is_goods_receipt_number_conflict(exc):
        raise HTTPException(
            status_code=409, detail="Goods receipt number already exists"
        ) from exc
    raise exc


def _actor(current_user: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc


def _validate_associations(
    db: Session,
    po_id: uuid.UUID,
    vendor_id: uuid.UUID,
    source_document_id: uuid.UUID | None,
    lines,
) -> None:
    po = db.get(PurchaseOrder, po_id)
    if po is None or not po.is_current:
        raise HTTPException(
            status_code=422, detail="Current purchase order was not found"
        )
    if po.vendor_id and po.vendor_id != vendor_id:
        raise HTTPException(
            status_code=422, detail="Goods receipt vendor does not match purchase order"
        )
    if db.get(Vendor, vendor_id) is None:
        raise HTTPException(status_code=422, detail="Vendor was not found")
    if source_document_id is not None:
        doc = db.get(Document, source_document_id)
        if doc is None:
            raise HTTPException(status_code=422, detail="Source document was not found")
    po_line_ids = set(
        db.scalars(
            select(PurchaseOrderLine.id).where(PurchaseOrderLine.po_id == po_id)
        ).all()
    )
    seen = set()
    for line in lines:
        if line.po_line_id not in po_line_ids:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Every goods receipt line must reference a line "
                    "on its purchase order"
                ),
            )
        if line.po_line_id in seen:
            raise HTTPException(
                status_code=422, detail="Duplicate purchase order lines are not allowed"
            )
        seen.add(line.po_line_id)


def _load(db: Session, receipt_id: uuid.UUID) -> GoodsReceipt:
    receipt = db.scalar(
        select(GoodsReceipt)
        .options(selectinload(GoodsReceipt.lines))
        .where(GoodsReceipt.id == receipt_id)
    )
    if receipt is None:
        raise HTTPException(status_code=404, detail="Goods receipt not found")
    return receipt


@router.get(
    "",
    response_model=list[GoodsReceiptResponse],
    dependencies=[Depends(get_current_user)],
)
def list_goods_receipts(
    db: Session = Depends(get_db),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    po_id: uuid.UUID | None = None,
) -> list[GoodsReceipt]:
    query = (
        select(GoodsReceipt)
        .options(selectinload(GoodsReceipt.lines))
        .order_by(GoodsReceipt.receipt_date.desc(), GoodsReceipt.id)
    )
    if po_id:
        query = query.where(GoodsReceipt.po_id == po_id)
    return list(db.scalars(query.offset((page - 1) * page_size).limit(page_size)).all())


@router.get(
    "/{receipt_id}",
    response_model=GoodsReceiptResponse,
    dependencies=[Depends(get_current_user)],
)
def get_goods_receipt(
    receipt_id: uuid.UUID, db: Session = Depends(get_db)
) -> GoodsReceipt:
    return _load(db, receipt_id)


@router.post(
    "",
    response_model=GoodsReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_user)],
)
def create_goods_receipt(
    payload: GoodsReceiptCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> GoodsReceipt:
    actor = _actor(current_user)
    if db.scalar(
        select(GoodsReceipt.id).where(
            func.lower(GoodsReceipt.gr_number) == payload.gr_number.strip().lower()
        )
    ):
        raise HTTPException(
            status_code=409, detail="Goods receipt number already exists"
        )
    _validate_associations(
        db, payload.po_id, payload.vendor_id, payload.source_document_id, payload.lines
    )
    receipt = GoodsReceipt(
        gr_number=payload.gr_number.strip(),
        po_id=payload.po_id,
        vendor_id=payload.vendor_id,
        source_document_id=payload.source_document_id,
        receipt_date=payload.receipt_date,
        created_by=actor,
        updated_by=actor,
        lines=[
            GoodsReceiptLine(**line.model_dump(), created_by=actor, updated_by=actor)
            for line in payload.lines
        ],
    )
    db.add(receipt)
    try:
        db.flush()
        db.add(
            AuditLog(
                action="goods_receipt_created",
                entity_type="goods_receipt",
                entity_id=receipt.id,
                new_value={"gr_number": receipt.gr_number, "po_id": str(receipt.po_id)},
                created_by=actor,
            )
        )
        db.commit()
    except IntegrityError as exc:
        _handle_goods_receipt_integrity_error(db, exc)
    return _load(db, receipt.id)


@router.patch(
    "/{receipt_id}",
    response_model=GoodsReceiptResponse,
    dependencies=[Depends(get_current_user)],
)
def update_goods_receipt(
    receipt_id: uuid.UUID,
    payload: GoodsReceiptUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> GoodsReceipt:
    receipt = _load(db, receipt_id)
    actor = _actor(current_user)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=422, detail="At least one goods receipt field must be provided"
        )
    lines = updates.pop("lines", None)
    po_id = updates.get("po_id", receipt.po_id)
    vendor_id = updates.get("vendor_id", receipt.vendor_id)
    source_document_id = updates.get("source_document_id", receipt.source_document_id)
    _validate_associations(
        db,
        po_id,
        vendor_id,
        source_document_id,
        lines if lines is not None else receipt.lines,
    )
    if "gr_number" in updates and db.scalar(
        select(GoodsReceipt.id).where(
            func.lower(GoodsReceipt.gr_number) == updates["gr_number"].strip().lower(),
            GoodsReceipt.id != receipt.id,
        )
    ):
        raise HTTPException(
            status_code=409, detail="Goods receipt number already exists"
        )
    old = {"gr_number": receipt.gr_number, "po_id": str(receipt.po_id)}
    for field, value in updates.items():
        setattr(receipt, field, value.strip() if field == "gr_number" else value)
    if lines is not None:
        receipt.lines.clear()
        receipt.lines.extend(
            GoodsReceiptLine(**line.model_dump(), created_by=actor, updated_by=actor)
            for line in lines
        )
    receipt.updated_by = actor
    try:
        db.flush()
        db.add(
            AuditLog(
                action="goods_receipt_updated",
                entity_type="goods_receipt",
                entity_id=receipt.id,
                old_value=old,
                new_value={"gr_number": receipt.gr_number, "po_id": str(receipt.po_id)},
                created_by=actor,
            )
        )
        db.commit()
    except IntegrityError as exc:
        _handle_goods_receipt_integrity_error(db, exc)
    return _load(db, receipt.id)


@router.delete(
    "/{receipt_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(get_current_user)],
)
def delete_goods_receipt(
    receipt_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> Response:
    receipt = _load(db, receipt_id)
    actor = _actor(current_user)
    db.add(
        AuditLog(
            action="goods_receipt_deleted",
            entity_type="goods_receipt",
            entity_id=receipt.id,
            old_value={"gr_number": receipt.gr_number, "po_id": str(receipt.po_id)},
            created_by=actor,
        )
    )
    db.delete(receipt)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
