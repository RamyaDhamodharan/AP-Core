"""
Application module for invoice supporting document schemas.
Flow: validated request and response data for the API flow.
Called by: src/api/rest/routers/invoice_supporting_document_routers.py.
Calls: standard-library or framework helpers.
Tables: no database tables directly.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

SupportingDocumentCategory = Literal[
    "delivery_note", "contract", "approval_email", "tax_certificate", "other"
]


class SupportingDocumentItem(BaseModel):
    """
    Represent supportingdocumentitem in the application flow.
    Fields: id, file_name, content_type, size_bytes, category, description, uploaded_by,
        uploaded_by_name, uploaded_at, deleted_at, deleted_by, deleted_reason.
    Use: shared persistence or validation contract for this flow stage.
    """
    id: UUID
    file_name: str
    content_type: str
    size_bytes: int
    category: SupportingDocumentCategory
    description: str | None
    uploaded_by: UUID
    uploaded_by_name: str | None
    uploaded_at: datetime
    deleted_at: datetime | None = None
    deleted_by: UUID | None = None
    deleted_reason: str | None = None
