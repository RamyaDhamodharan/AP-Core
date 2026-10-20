import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GoodsReceiptLineInput(BaseModel):
    po_line_id: uuid.UUID
    item_code: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=300)
    uom: str | None = Field(default=None, max_length=10)
    received_qty: Decimal = Field(gt=0, max_digits=14, decimal_places=3)
    accepted_qty: Decimal = Field(ge=0, max_digits=14, decimal_places=3)
    rejected_qty: Decimal = Field(
        default=Decimal("0"), ge=0, max_digits=14, decimal_places=3
    )

    @model_validator(mode="after")
    def quantities_balance(self):
        if self.accepted_qty + self.rejected_qty != self.received_qty:
            raise ValueError("accepted_qty plus rejected_qty must equal received_qty")
        return self


class GoodsReceiptCreate(BaseModel):
    gr_number: str = Field(min_length=1, max_length=60)
    po_id: uuid.UUID
    vendor_id: uuid.UUID
    source_document_id: uuid.UUID | None = None
    receipt_date: date
    lines: list[GoodsReceiptLineInput] = Field(min_length=1)


class GoodsReceiptUpdate(BaseModel):
    gr_number: str | None = Field(default=None, min_length=1, max_length=60)
    po_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    source_document_id: uuid.UUID | None = None
    receipt_date: date | None = None
    lines: list[GoodsReceiptLineInput] | None = Field(default=None, min_length=1)


class GoodsReceiptLineResponse(GoodsReceiptLineInput):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class GoodsReceiptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    gr_number: str
    po_id: uuid.UUID
    vendor_id: uuid.UUID
    source_document_id: uuid.UUID | None
    receipt_date: date
    created_at: datetime
    updated_at: datetime
    lines: list[GoodsReceiptLineResponse]
