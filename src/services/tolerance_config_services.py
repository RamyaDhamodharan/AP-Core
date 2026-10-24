"""
Validated percentage tolerance configuration shared by API and workers.
Flow: shared business-flow service called by API routes and workers.
Called by: src/api/rest/app.py, src/services/document_workflow_services.py,
src/worker/celery_app.py.
Calls: standard-library or framework helpers.
Tables: no database tables directly.
"""

import os
from decimal import Decimal, InvalidOperation

MAX_TOLERANCE_PERCENT = Decimal("2")


def read_tolerance_percent(name: str, default: str) -> Decimal:
    """
    Read a percentage value in [0, 2], raising a startup-friendly error.
    Inputs: name (str), default (str).
    Output: Decimal.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    raw = os.getenv(name, default)
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number between 0 and 2") from exc
    if not value.is_finite() or value < 0 or value > MAX_TOLERANCE_PERCENT:
        raise ValueError(f"{name} must be between 0 and 2 percent")
    return value


def read_nonnegative_decimal(name: str, default: str) -> Decimal:
    """
    Handle read nonnegative decimal.
    Inputs: name (str), default (str).
    Output: Decimal.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    raw = os.getenv(name, default)
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative number") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return value


def tolerance_fraction(name: str, default: str) -> Decimal:
    """
    Handle tolerance fraction.
    Inputs: name (str), default (str).
    Output: Decimal.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    return read_tolerance_percent(name, default) / Decimal(100)


def validate_tolerance_config() -> None:
    """
    Validate validate tolerance config.
    Inputs: no explicit inputs.
    Output: None.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    for name, default in (
        ("INVOICE_UNIT_PRICE_TOLERANCE_PCT", "2"),
        ("TAX_TOLERANCE_PERCENT", "0.5"),
        ("INVOICE_TOTAL_TOLERANCE_PCT", "1"),
    ):
        read_tolerance_percent(name, default)
    read_nonnegative_decimal("TAX_TOLERANCE_ABS", "0.01")
