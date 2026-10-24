"""
Application module for audit models.
Flow: shared database records used through intake, matching, and review.
Called by: src/api/rest/routers/audit_routers.py,
src/api/rest/routers/command_center_routers.py,
src/api/rest/routers/document_routers.py.
Calls: vendor_models.
Tables: audit_log.
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.vendor_models import Base

STEP_RESULT_VALUES = ("success", "warning", "error")


class AuditLog(Base):
    """
    Represent the audit_log table used by the AP workflow.
    Table: audit_log.
    Status values: this table has no status enum.
    Use: shared persistence or validation contract for this flow stage.
    """
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_entity_timeline", "entity_type", "entity_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[str] = mapped_column(
        ENUM(
            *STEP_RESULT_VALUES,
            name="step_result",
            schema="core",
            create_type=False,
        ),
        nullable=False,
        server_default=text("'success'"),
    )
    message: Mapped[str | None] = mapped_column(String(500))
    reason: Mapped[str | None] = mapped_column(Text)
    step_name: Mapped[str | None] = mapped_column(String(60))
    duration_ms: Mapped[int | None] = mapped_column()
    input_ref: Mapped[str | None] = mapped_column(String(500))
    output_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    technical_detail: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth.users.id"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth.users.id"),
    )


@event.listens_for(AuditLog, "before_insert")
def assign_audit_actor(_mapper, _connection, target: AuditLog) -> None:
    """
    Make system and user actors explicit on every newly written audit row.
    Inputs: _mapper, _connection, target (AuditLog).
    Output: None.
    Effects: may add audit entries.
    Results: no workflow status or reason code directly.
    """
    if target.actor is None:
        target.actor = str(target.created_by) if target.created_by else "system"
