import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DocumentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    status: str
    doc_type: str | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime


class DocumentDetail(DocumentSummary):
    raw_text: str | None
    extracted_json: dict[str, Any] | None


class DocumentListResponse(BaseModel):
    items: list[DocumentSummary]
    total: int
    page: int
    page_size: int


class DocumentAuditEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action: str
    result: str
    message: str | None
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    created_by: uuid.UUID | None
    created_at: datetime


class DocumentReviewDetail(DocumentDetail):
    audit_history: list[DocumentAuditEntry]
