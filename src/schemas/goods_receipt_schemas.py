"""
Application module for goods receipt schemas.
Flow: validated request and response data for the API flow.
Called by: src/api/rest/routers/goods_receipt_routers.py.
Calls: standard-library or framework helpers.
Tables: no database tables directly.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GoodsReceiptLineInput(BaseModel):
    """
    Represent goodsreceiptlineinput in the application flow.
    Fields: po_line_id, item_code, description, uom, received_qty, accepted_qty,
        rejected_qty.
    Use: shared persistence or validation contract for this flow stage.
    """
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
        """
        Handle quantities balance.
        Inputs: no explicit inputs.
        Output: the value or None returned by its body.
        Effects: does not write database rows or queue work directly.
        Results: no workflow status or reason code directly.
        """
        if (
            self.received_qty is None
            or self.accepted_qty is None
            or self.rejected_qty is None
        ):
            return self
        if self.accepted_qty + self.rejected_qty != self.received_qty:
            raise ValueError("accepted_qty plus rejected_qty must equal received_qty")
        return self


class GoodsReceiptCreate(BaseModel):
    """
    Represent goodsreceiptcreate in the application flow.
    Fields: gr_number, po_id, vendor_id, source_document_id, receipt_date, lines.
    Use: shared persistence or validation contract for this flow stage.
    """
    gr_number: str = Field(min_length=1, max_length=60)
    po_id: uuid.UUID
    vendor_id: uuid.UUID
    source_document_id: uuid.UUID | None = None
    receipt_date: date
    lines: list[GoodsReceiptLineInput] = Field(min_length=1)


class GoodsReceiptUpdate(BaseModel):
    """
    Represent goodsreceiptupdate in the application flow.
    Fields: gr_number, po_id, vendor_id, source_document_id, receipt_date, lines.
    Use: shared persistence or validation contract for this flow stage.
    """
    gr_number: str | None = Field(default=None, min_length=1, max_length=60)
    po_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    source_document_id: uuid.UUID | None = None
    receipt_date: date | None = None
    lines: list[GoodsReceiptLineInput] | None = Field(default=None, min_length=1)


class GoodsReceiptLineResponse(GoodsReceiptLineInput):
    """
    Validate the goodsreceiptlineresponse API data shape.
    Fields: id, po_line_id, received_qty, accepted_qty, rejected_qty, field_confidence.
    Use: shared persistence or validation contract for this flow stage.
    """
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    po_line_id: uuid.UUID | None
    received_qty: Decimal | None
    accepted_qty: Decimal | None
    rejected_qty: Decimal | None
    field_confidence: dict | None = None


class GoodsReceiptResponse(BaseModel):
    """
    Validate the goodsreceiptresponse API data shape.
    Fields: id, gr_number, po_id, vendor_id, vendor_name, vendor_pending,
        verification_status, source_document_id, receipt_date, field_confidence,
        needs_verification, created_at, and more
    Use: shared persistence or validation contract for this flow stage.
    """
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    gr_number: str
    po_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    vendor_name: str | None = None
    vendor_pending: bool = False
    verification_status: str = "verified"
    source_document_id: uuid.UUID | None
    receipt_date: date | None
    field_confidence: dict | None = None
    needs_verification: bool = False
    created_at: datetime
    updated_at: datetime
    lines: list[GoodsReceiptLineResponse]
