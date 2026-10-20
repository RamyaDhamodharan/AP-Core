import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class PurchaseOrderLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    po_id: uuid.UUID
    item_code: str | None
    description: str | None
    uom: str | None
    qty: Decimal | None
    unit_price: Decimal | None
    line_amount: Decimal | None


class PurchaseOrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    po_number: str
    version_no: int
    is_current: bool
    vendor_id: uuid.UUID | None
    vendor_name: str | None
    source_document_id: uuid.UUID | None
    po_date: date | None
    currency: str
    subtotal: Decimal | None
    tax_amount: Decimal | None
    total_amount: Decimal | None
    created_at: datetime
    updated_at: datetime
    lines: list[PurchaseOrderLineResponse]


class PurchaseOrderListResponse(BaseModel):
    items: list[PurchaseOrderResponse]
    total: int
    page: int
    page_size: int
