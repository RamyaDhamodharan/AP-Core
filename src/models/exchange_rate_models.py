"""
Application module for exchange rate models.
Flow: shared database records used through intake, matching, and review.
Called by: the framework, CLI, or application entry point.
Calls: vendor_models.
Tables: no database tables directly.
"""
import uuid

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.vendor_models import Base


class ExchangeRate(Base):
    """
    Represent the exchange_rates table used by the AP workflow.
    Table: exchange_rates.
    Status values: this table has no status enum.
    Use: shared persistence or validation contract for this flow stage.
    """
    __tablename__ = "exchange_rates"
    __table_args__ = (
        UniqueConstraint(
            "from_currency",
            "to_currency",
            "rate_date",
            name="uq_exchange_rate",
        ),
        CheckConstraint("rate > 0", name="exchange_rates_rate_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    from_currency = mapped_column(CHAR(3), nullable=False)
    to_currency = mapped_column(CHAR(3), nullable=False)
    rate = mapped_column(Numeric(14, 6), nullable=False)
    rate_date = mapped_column(Date, nullable=False)

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
