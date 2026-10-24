"""
Application module for invoice issue models.
Flow: shared database records used through intake, matching, and review.
Called by: src/api/rest/routers/command_center_routers.py,
src/api/rest/routers/email_draft_routers.py,
src/api/rest/routers/invoice_routers.py.
Calls: vendor_models.
Tables: no database tables directly.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.vendor_models import Base


class InvoiceIssue(Base):
    """
    Represent the invoice_issues table used by the AP workflow.
    Table: invoice_issues.
    Status values: this table has no status enum.
    Use: shared persistence or validation contract for this flow stage.
    """
    __tablename__ = "invoice_issues"
    __table_args__ = (
        Index("ix_invoice_issues_status_due", "status", "due_at"),
        Index("ix_invoice_issues_owner_status", "owner_user_id", "status"),
        Index(
            "uq_invoice_issue_open_type",
            "invoice_id",
            "issue_type",
            unique=True,
            postgresql_where=text(
                "status IN ('open','in_progress','waiting_vendor','escalated')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    issue_type: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'warning'")
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    expected_value: Mapped[dict | None] = mapped_column(JSONB)
    actual_value: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'open'")
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)


class IssueEvent(Base):
    """
    Represent the issue_events table used by the AP workflow.
    Table: issue_events.
    Status values: this table has no status enum.
    Use: shared persistence or validation contract for this flow stage.
    """
    __tablename__ = "issue_events"
    __table_args__ = (Index("ix_issue_events_timeline", "issue_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice_issues.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
