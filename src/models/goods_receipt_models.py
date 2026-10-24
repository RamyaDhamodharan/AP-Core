"""
Application module for goods receipt models.
Flow: shared database records used through intake, matching, and review.
Called by: scripts/seed_demo.py, scripts/seed_import.py,
scripts/seed_invoice_review_sample.py.
Calls: vendor_models.
Tables: goods_receipts.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.vendor_models import Base, Vendor


class GoodsReceipt(Base):
    """
    Represent the goods_receipts table used by the AP workflow.
    Table: goods_receipts.
    Status values: this table has no status enum.
    Use: shared persistence or validation contract for this flow stage.
    """
    __tablename__ = "goods_receipts"
    __table_args__ = (
        UniqueConstraint("gr_number", name="uq_goods_receipts_gr_number"),
        Index(
            "uq_goods_receipts_gr_number_norm",
            text("lower(TRIM(BOTH FROM gr_number))"),
            unique=True,
        ),
        Index(
            "uq_goods_receipts_source_document",
            "source_document_id",
            unique=True,
            postgresql_where=text("source_document_id IS NOT NULL"),
        ),
        Index("ix_goods_receipts_po_id", "po_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    gr_number: Mapped[str] = mapped_column(String(60), nullable=False)
    po_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id"), nullable=True
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=True
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id")
    )
    receipt_date: Mapped[date | None] = mapped_column(Date)
    field_confidence: Mapped[dict | None] = mapped_column(JSONB)
    needs_verification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
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
    lines: Mapped[list["GoodsReceiptLine"]] = relationship(
        back_populates="goods_receipt", cascade="all, delete-orphan", lazy="selectin"
    )
    vendor: Mapped["Vendor | None"] = relationship("Vendor", lazy="joined")

    @property
    def vendor_name(self) -> str | None:
        """
        Handle vendor name.
        Inputs: no explicit inputs.
        Output: str | None.
        Effects: does not write database rows or queue work directly.
        Results: no workflow status or reason code directly.
        """
        return self.vendor.name if self.vendor is not None else None

    @property
    def vendor_pending(self) -> bool:
        """
        Handle vendor pending.
        Inputs: no explicit inputs.
        Output: bool.
        Effects: does not write database rows or queue work directly.
        Results: no workflow status or reason code directly.
        """
        return self.vendor_id is None

    @property
    def verification_status(self) -> str:
        """
        Handle verification status.
        Inputs: no explicit inputs.
        Output: str.
        Effects: does not write database rows or queue work directly.
        Results: no workflow status or reason code directly.
        """
        return (
            "needs_verification"
            if self.needs_verification or self.vendor_id is None
            else "verified"
        )


class GoodsReceiptLine(Base):
    """
    Represent the goods_receipt_lines table used by the AP workflow.
    Table: goods_receipt_lines.
    Status values: this table has no status enum.
    Use: shared persistence or validation contract for this flow stage.
    """
    __tablename__ = "goods_receipt_lines"
    __table_args__ = (
        CheckConstraint("received_qty > 0", name="ck_gr_received_qty_positive"),
        CheckConstraint("accepted_qty >= 0", name="ck_gr_accepted_qty_nonnegative"),
        CheckConstraint("rejected_qty >= 0", name="ck_gr_rejected_qty_nonnegative"),
        CheckConstraint(
            "accepted_qty + rejected_qty = received_qty", name="ck_gr_line_qty_balance"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("goods_receipts.id", ondelete="CASCADE"),
        nullable=False,
    )
    po_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("po_lines.id"), nullable=True
    )
    item_code: Mapped[str | None] = mapped_column(String(60))
    description: Mapped[str | None] = mapped_column(String(300))
    uom: Mapped[str | None] = mapped_column(String(10))
    received_qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    accepted_qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    rejected_qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    field_confidence: Mapped[dict | None] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
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
    goods_receipt: Mapped[GoodsReceipt] = relationship(back_populates="lines")
