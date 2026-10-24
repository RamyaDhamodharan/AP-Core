"""
Application module for match run models.
Flow: shared database records used through intake, matching, and review.
Called by: src/api/rest/routers/command_center_routers.py,
src/api/rest/routers/document_routers.py, src/api/rest/routers/invoice_routers.py.
Calls: vendor_models.
Tables: no database tables directly.
"""
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.vendor_models import Base


class MatchRun(Base):
    """
    Represent the match_runs table used by the AP workflow.
    Table: match_runs.
    Status values: this table has no status enum.
    Use: shared persistence or validation contract for this flow stage.
    """
    __tablename__ = "match_runs"
    __table_args__ = (UniqueConstraint("invoice_id", "run_no", name="uq_match_run_no"),)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_no: Mapped[int] = mapped_column(Integer, nullable=False)
    po_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("purchase_orders.id"),
    )
    result = mapped_column(
        ENUM(name="match_result", schema="core", create_type=False),
        nullable=False,
    )
    exchange_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("exchange_rates.id"),
    )
    rules_used = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    differences = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )

    created_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_by = mapped_column(UUID(as_uuid=True), ForeignKey("auth.users.id"))
    updated_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth.users.id"),
    )
