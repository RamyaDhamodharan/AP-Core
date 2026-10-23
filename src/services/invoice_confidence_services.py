"""Normalization helpers for extraction confidence and verification gating."""

import os
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
