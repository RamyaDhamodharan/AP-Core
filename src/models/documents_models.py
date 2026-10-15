
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, Text, func, text
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.vendor import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    original_filename: Mapped[str] = mapped_column(String(300), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(500))
    parent_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    status: Mapped[str] = mapped_column(
    ENUM(
        "received",
        "parsing",
        "parsed",
        "classified",
        "extracted",
        "needs_vendor_review",
        "processed",
        "ignored",
        "incomplete",
        "duplicate_blocked",
        "failed",
        name="doc_status",
        create_type=False,
    ),
    nullable=False,
    server_default=text("'received'"),
    )

    doc_type: Mapped[str | None] = mapped_column(
        ENUM(
            "po",
            "invoice",
            "other",
            name="doc_type",
            create_type=False,
        ),
        nullable=True,
    )
    raw_text: Mapped[str | None] = mapped_column(Text)
    extracted_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    email_sender: Mapped[str | None] = mapped_column(String(200))
    email_subject: Mapped[str | None] = mapped_column(String(500))
    email_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
