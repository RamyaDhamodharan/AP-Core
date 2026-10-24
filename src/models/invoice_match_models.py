"""
Application module for invoice match models.
Flow: shared database records used through intake, matching, and review.
Called by: src/api/rest/routers/document_routers.py,
src/api/rest/routers/invoice_routers.py, src/services/invoice_matching_services.py.
Calls: vendor_models.
Tables: no database tables directly.
"""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.vendor_models import Base


class InvoiceMatch(Base):
    """
    Represent the invoice_matches table used by the AP workflow.
    Table: invoice_matches.
    Status values: this table has no status enum.
    Use: shared persistence or validation contract for this flow stage.
    """
    __tablename__ = "invoice_matches"
    __table_args__ = (Index("ix_invoice_matches_invoice", "invoice_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    po_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id")
    )
    gr_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goods_receipts.id")
    )
    match_type: Mapped[str] = mapped_column(String(10), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="suggested"
    )
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class InvoiceDiscrepancy(Base):
    """
    Represent the invoice_discrepancies table used by the AP workflow.
    Table: invoice_discrepancies.
    Status values: this table has no status enum.
    Use: shared persistence or validation contract for this flow stage.
    """
    __tablename__ = "invoice_discrepancies"
    __table_args__ = (
        Index("ix_invoice_discrepancies_invoice", "invoice_id", "resolved"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    field: Mapped[str] = mapped_column(String(80), nullable=False)
    invoice_value: Mapped[Any | None] = mapped_column(JSONB)
    po_value: Mapped[Any | None] = mapped_column(JSONB)
    gr_value: Mapped[Any | None] = mapped_column(JSONB)
    severity: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="warning"
    )
    resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
