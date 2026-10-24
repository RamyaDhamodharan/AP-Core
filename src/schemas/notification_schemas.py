"""
Application module for notification schemas.
Flow: validated request and response data for the API flow.
Called by: src/api/rest/routers/notification_routers.py.
Calls: standard-library or framework helpers.
Tables: no database tables directly.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

NotificationStatus = Literal["queued", "sent", "failed"]
InvoiceExceptionType = Literal[
    "match_issues",
    "waiting_for_po",
    "needs_vendor_review",
    "pending_review",
]


class NotificationItem(BaseModel):
    """
    Represent notificationitem in the application flow.
    Fields: notification_id, document_id, invoice_id, invoice_no, exception_type,
        status, subject, body, created_at.
    Use: shared persistence or validation contract for this flow stage.
    """
    notification_id: UUID
    document_id: UUID
    invoice_id: UUID | None
    invoice_no: str | None
    exception_type: InvoiceExceptionType | Literal["notification"]
    status: NotificationStatus
    subject: str | None
    body: str | None
    created_at: datetime


class NotificationListResponse(BaseModel):
    """
    Validate the notificationlistresponse API data shape.
    Fields: items, total, page, page_size.
    Use: shared persistence or validation contract for this flow stage.
    """
    items: list[NotificationItem]
    total: int
    page: int
    page_size: int


class ExceptionAlertItem(BaseModel):
    """
    Represent exceptionalertitem in the application flow.
    Fields: invoice_id, invoice_no, exception_type, status, final_status, created_at.
    Use: shared persistence or validation contract for this flow stage.
    """
    invoice_id: UUID
    invoice_no: str | None
    exception_type: InvoiceExceptionType
    status: str
    final_status: str | None
    created_at: datetime


class ExceptionAlertListResponse(BaseModel):
    """
    Validate the exceptionalertlistresponse API data shape.
    Fields: items, total, page, page_size.
    Use: shared persistence or validation contract for this flow stage.
    """
    items: list[ExceptionAlertItem]
    total: int
    page: int
    page_size: int
