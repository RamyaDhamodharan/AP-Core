from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

InvoiceMatchStatus = Literal[
    "pending",
    "needs_vendor_review",
    "waiting_for_po",
    "needs_po_confirmation",
    "ready_for_review",
    "match_issues",
    "no_match",
]
InvoiceFinalStatus = Literal["approved", "rejected", "on_hold"]


class InvoiceReviewRequest(BaseModel):
    decision: InvoiceFinalStatus


class InvoiceReviewResponse(BaseModel):
    invoice_id: UUID
    final_status: InvoiceFinalStatus
    final_by: UUID
    final_at: datetime


class InvoiceDashboardItem(BaseModel):
    invoice_id: UUID
    invoice_no: str | None
    vendor_name: str | None
    invoice_date: date | None
    due_date: date | None
    total_amount: Decimal | None
    currency: str | None
    match_status: InvoiceMatchStatus
    final_status: InvoiceFinalStatus | None


class InvoiceDashboardResponse(BaseModel):
    items: list[InvoiceDashboardItem]
    total: int
    page: int
    page_size: int
