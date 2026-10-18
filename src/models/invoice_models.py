import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.vendor_models import Base


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"),
    )
    invoice_no: Mapped[str | None] = mapped_column(String(60))
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id"),
    )
    vendor_name_raw: Mapped[str | None] = mapped_column(String(200))
    vendor_tax_id_raw: Mapped[str | None] = mapped_column(String(30))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(String(3))
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    po_number_raw: Mapped[str | None] = mapped_column(String(60))
    po_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id"),
    )
    match_status: Mapped[str] = mapped_column(
        ENUM(
            "pending", "needs_vendor_review", "waiting_for_po",
            "needs_po_confirmation", "ready_for_review",
            "match_issues", "no_match",
            name="match_status", create_type=False,
        ),
        nullable=False, server_default=text("'pending'"),
    )
    final_status: Mapped[str | None] = mapped_column(
        ENUM(
            "approved", "rejected", "on_hold",
            name="review_decision", create_type=False,
        ),
    )
    final_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    final_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
    )
    final_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
    )
