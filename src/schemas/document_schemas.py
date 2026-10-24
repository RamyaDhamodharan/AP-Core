"""
Application module for document schemas.
Flow: validated request and response data for the API flow.
Called by: src/api/rest/routers/document_routers.py,
src/api/rest/routers/invoice_supporting_document_routers.py.
Calls: standard-library or framework helpers.
Tables: no database tables directly.
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DocumentSummary(BaseModel):
    """
    Represent documentsummary in the application flow.
    Fields: id, original_filename, status, doc_type, failure_message, failure_class,
        retry_count, next_retry_at, last_attempt_at, source, parent_document_id,
        email_sender, and more
    Use: shared persistence or validation contract for this flow stage.
    """
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    status: str
    doc_type: str | None
    failure_message: str | None
    failure_class: str | None = None
    retry_count: int = 0
    next_retry_at: datetime | None = None
    last_attempt_at: datetime | None = None
    source: str | None = None
    parent_document_id: uuid.UUID | None = None
    email_sender: str | None = None
    email_subject: str | None = None
    email_received_at: datetime | None = None
    email_message_id: str | None = None
    linked_invoice_id: uuid.UUID | None = None
    linked_invoice_status: str | None = None
    created_at: datetime
    updated_at: datetime


class DocumentDetail(DocumentSummary):
    """
    Represent documentdetail in the application flow.
    Fields: raw_text, extracted_json, linked_po_id, linked_gr_id.
    Use: shared persistence or validation contract for this flow stage.
    """
    raw_text: str | None
    extracted_json: dict[str, Any] | None
    linked_po_id: uuid.UUID | None = None
    linked_gr_id: uuid.UUID | None = None


class DocumentListResponse(BaseModel):
    """
    Validate the documentlistresponse API data shape.
    Fields: items, total, page, page_size.
    Use: shared persistence or validation contract for this flow stage.
    """
    items: list[DocumentSummary]
    total: int
    page: int
    page_size: int


class DocumentAuditEntry(BaseModel):
    """
    Represent documentauditentry in the application flow.
    Fields: action, result, message, old_value, new_value, step_name, duration_ms,
        input_ref, output_summary, reason, created_by, created_at.
    Use: shared persistence or validation contract for this flow stage.
    """
    model_config = ConfigDict(from_attributes=True)

    action: str
    result: str
    message: str | None
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    step_name: str | None = None
    duration_ms: int | None = None
    input_ref: str | None = None
    output_summary: dict[str, Any] | None = None
    reason: str | None = None
    created_by: uuid.UUID | None
    created_at: datetime


class DocumentReviewDetail(DocumentDetail):
    """
    Represent documentreviewdetail in the application flow.
    Fields: audit_history.
    Use: shared persistence or validation contract for this flow stage.
    """
    audit_history: list[DocumentAuditEntry]
