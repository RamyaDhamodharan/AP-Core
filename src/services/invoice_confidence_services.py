"""Normalization helpers for extraction confidence and verification gating."""

import os
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

INVOICE_CONFIDENCE_FIELDS = (
    "vendor",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "subtotal",
    "tax",
    "total",
    "order_number",
    "gl_code",
)
LINE_CONFIDENCE_FIELDS = ("qty", "unit_price", "line_amount")


def overall_confidence_percent(confidence: dict[str, Any] | None) -> int:
    """Return the deterministic invoice confidence percentage.

    Formula: take the minimum score across the ten material header fields;
    when line items exist, average every material line-field score and take
    the lower of the header minimum and that line average. Missing or invalid
    values score 0. Scores in the 0..1 range are treated as fractions.
    The final percentage is rounded half up to an integer.
    """
    confidence = confidence if isinstance(confidence, dict) else {}

    def score(raw: Any) -> Decimal:
        value = (
            raw.get("confidence", raw.get("score"))
            if isinstance(raw, dict)
            else raw
        )
        try:
            result = Decimal(str(value))
            if Decimal("0") <= result <= Decimal("1"):
                result *= Decimal("100")
            return min(Decimal("100"), max(Decimal("0"), result))
        except Exception:
            return Decimal("0")

    header_scores = [
        score(confidence.get(field)) for field in INVOICE_CONFIDENCE_FIELDS
    ]
    overall = min(header_scores, default=Decimal("0"))
    lines = confidence.get("line_items")
    if isinstance(lines, list) and lines:
        line_scores = [
            score(line.get(field)) if isinstance(line, dict) else Decimal("0")
            for line in lines
            for field in LINE_CONFIDENCE_FIELDS
        ]
        overall = min(
            overall, sum(line_scores, Decimal("0")) / Decimal(len(line_scores))
        )
    return int(overall.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def confidence_threshold() -> float:
    try:
        return min(100.0, max(0.0, float(os.getenv("FIELD_CONFIDENCE_MIN", "85"))))
    except ValueError:
        return 85.0


def _confidence_entry(value: Any, present: bool) -> dict[str, Any]:
    raw = value
    derived = False
    if isinstance(value, dict):
        raw = value.get("confidence", value.get("score"))
        derived = bool(value.get("derived", False))
    try:
        score = float(raw)
        if 0 <= score <= 1:
            score *= 100
        score = min(100.0, max(0.0, score))
    except (TypeError, ValueError):
        score = 0.0
        derived = True
    return {"confidence": score, "derived": derived}


def normalize_invoice_confidence(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("field_confidence") or data.get("confidence") or {}
    raw = raw if isinstance(raw, dict) else {}
    normalized = {
        field: _confidence_entry(raw.get(field), data.get(field) is not None)
        for field in INVOICE_CONFIDENCE_FIELDS
    }
    lines = data.get("line_items")
    normalized["line_items"] = [
        {
            field: _confidence_entry(
                (line.get("field_confidence") or {}).get(field)
                if isinstance(line.get("field_confidence"), dict)
                else None,
                line.get(field) is not None,
            )
            for field in LINE_CONFIDENCE_FIELDS
        }
        for line in lines or []
        if isinstance(line, dict)
    ]
    return normalized


def requires_verification(confidence: dict[str, Any]) -> bool:
    if not confidence:
        return True
    threshold = confidence_threshold()
    present_fields = {
        field for field in INVOICE_CONFIDENCE_FIELDS if field in confidence
    }
    return (
        len(present_fields) != len(INVOICE_CONFIDENCE_FIELDS)
        or any(
            entry["confidence"] < threshold
            for field, entry in confidence.items()
            if field != "line_items" and isinstance(entry, dict)
        )
        or any(
            entry[field]["confidence"] < threshold
            for entry in confidence.get("line_items", [])
            for field in LINE_CONFIDENCE_FIELDS
        )
    )


def confidence_unavailable(confidence: dict[str, Any] | None) -> bool:
    if not confidence or any(
        field not in confidence for field in INVOICE_CONFIDENCE_FIELDS
    ):
        return True
    if any(
        not isinstance(confidence.get(field), dict)
        or confidence[field].get("confidence") is None
        or confidence[field].get("derived", False)
        for field in INVOICE_CONFIDENCE_FIELDS
    ):
        return True
    for line in confidence.get("line_items", []):
        if any(
            field not in line
            or not isinstance(line[field], dict)
            or line[field].get("confidence") is None
            or line[field].get("derived", False)
            for field in LINE_CONFIDENCE_FIELDS
        ):
            return True
    return False


def confidence_level(confidence: dict[str, Any] | None) -> str:
    """Return a display-safe confidence label without exposing a score."""
    if not confidence:
        return "unknown"
    if any(field not in confidence for field in INVOICE_CONFIDENCE_FIELDS):
        return "unknown"
    entries = [confidence[field] for field in INVOICE_CONFIDENCE_FIELDS]
    for line in confidence.get("line_items", []):
        if any(field not in line for field in LINE_CONFIDENCE_FIELDS):
            return "unknown"
        entries.extend(line[field] for field in LINE_CONFIDENCE_FIELDS)
    if any(
        not isinstance(entry, dict)
        or entry.get("confidence") is None
        or entry.get("derived", False)
        for entry in entries
    ):
        return "unknown"
    scores = [float(entry["confidence"]) for entry in entries]
    lowest = min(scores)
    if lowest >= 90:
        return "high"
    if lowest >= 75:
        return "medium"
    return "low"
