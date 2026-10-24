"""
Application module for purchase order schemas.
Flow: validated request and response data for the API flow.
Called by: src/api/rest/routers/document_routers.py,
src/api/rest/routers/purchase_order_routers.py.
Calls: vendor_schemas.
Tables: no database tables directly.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.schemas.vendor_schemas import VendorCreate


class PurchaseOrderLineResponse(BaseModel):
    """
    Validate the purchaseorderlineresponse API data shape.
    Fields: id, po_id, item_code, description, uom, qty, unit_price, line_amount,
        field_confidence.
    Use: shared persistence or validation contract for this flow stage.
    """
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    po_id: uuid.UUID
    item_code: str | None
    description: str | None
    uom: str | None
    qty: Decimal | None
    unit_price: Decimal | None
    line_amount: Decimal | None
    field_confidence: dict[str, Any] | None = None


class PurchaseOrderResponse(BaseModel):
    """
    Validate the purchaseorderresponse API data shape.
    Fields: id, po_number, version_no, is_current, vendor_id, vendor_name,
        source_document_id, po_date, currency, subtotal, tax_amount, total_amount, and
        more
    Use: shared persistence or validation contract for this flow stage.
    """
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    po_number: str
    version_no: int
    is_current: bool
    vendor_id: uuid.UUID | None
    vendor_name: str | None
    source_document_id: uuid.UUID | None
    po_date: date | None
    currency: str | None
    subtotal: Decimal | None
    tax_amount: Decimal | None
    total_amount: Decimal | None
    payment_terms: Any | None = None
    delivery_details: Any | None = None
    field_confidence: dict[str, Any] | None = None
    needs_verification: bool = False
    vendor_pending: bool = False
    verification_status: str = "verified"
    created_at: datetime
    updated_at: datetime
    lines: list[PurchaseOrderLineResponse]


class PurchaseOrderListResponse(BaseModel):
    """
    Validate the purchaseorderlistresponse API data shape.
    Fields: items, total, page, page_size, counts.
    Use: shared persistence or validation contract for this flow stage.
    """
    items: list[PurchaseOrderResponse]
    total: int
    page: int
    page_size: int
    counts: dict[str, int] = Field(default_factory=dict)


class PurchaseOrderVendorResolutionRequest(BaseModel):
    """
    Validate the purchaseordervendorresolutionrequest API data shape.
    Fields: vendor_id, new_vendor.
    Use: shared persistence or validation contract for this flow stage.
    """
    vendor_id: uuid.UUID | None = None
    new_vendor: VendorCreate | None = None

    @model_validator(mode="after")
    def validate_vendor_choice(self):
        """
        Validate validate vendor choice.
        Inputs: no explicit inputs.
        Output: the value or None returned by its body.
        Effects: does not write database rows or queue work directly.
        Results: no workflow status or reason code directly.
        """
        if (self.vendor_id is None) == (self.new_vendor is None):
            raise ValueError("Select an existing vendor or provide a new vendor")
        if self.new_vendor and not any(
            (
                self.new_vendor.tax_id,
                self.new_vendor.contact_email,
                self.new_vendor.contact_phone,
            )
        ):
            raise ValueError(
                "A new vendor requires a tax ID, contact email, or contact phone"
            )
        return self


class PurchaseOrderVendorReassignmentRequest(BaseModel):
    """
    Validate the purchaseordervendorreassignmentrequest API data shape.
    Fields: vendor_id.
    Use: shared persistence or validation contract for this flow stage.
    """
    vendor_id: uuid.UUID


class PurchaseOrderAmendmentLine(BaseModel):
    item_code: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=300)
    uom: str | None = Field(default=None, max_length=10)
    qty: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=3)
    unit_price: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=4
    )
    line_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )


class PurchaseOrderAmendmentRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    po_date: date | None = None
    currency: str | None = Field(default=None, pattern="^[A-Z]{3}$")
    subtotal: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    tax_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    tax_rate: Decimal | None = Field(default=None, ge=0, max_digits=7, decimal_places=4)
    total_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    payment_terms: Any | None = None
    delivery_details: Any | None = None
    lines: list[PurchaseOrderAmendmentLine] | None = Field(
        default=None, min_length=1, max_length=200
    )
