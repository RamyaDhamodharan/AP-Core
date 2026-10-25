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
CORE_EXTRACTION_FIELDS = (
    "vendor",
    "invoice_number",
    "invoice_date",
    "total",
)
LINE_CONFIDENCE_FIELDS = ("qty", "unit_price", "line_amount")


def overall_confidence_percent(confidence: dict[str, Any] | None) -> int:
    """Return the invoice confidence percentage.

    If an explicit overall match or LLM confidence is present, respect it directly.
    Otherwise, compute average across present core material fields.
    """
    confidence = confidence if isinstance(confidence, dict) else {}

    # Check for direct LLM confidence score
    for key in ("overall_match_confidence", "overall_confidence"):
        if key in confidence:
            val = confidence[key]
            raw = val.get("confidence", val.get("score")) if isinstance(val, dict) else val
            try:
                score_num = Decimal(str(raw))
                if Decimal("0") <= score_num <= Decimal("1"):
                    score_num *= Decimal("100")
                return int(score_num.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            except Exception:
                pass

    def score(raw: Any) -> Decimal | None:
        if raw is None:
            return None
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
            return None

    # Evaluate material fields (excluding internal ERP codes like gl_code)
    eval_fields = [f for f in INVOICE_CONFIDENCE_FIELDS if f not in ("gl_code", "order_number")]
    scores = []
    for field in eval_fields:
        s = score(confidence.get(field))
        if s is not None:
            scores.append(s)

    lines = confidence.get("line_items")
    if isinstance(lines, list) and lines:
        for line in lines:
            if isinstance(line, dict):
                for lf in LINE_CONFIDENCE_FIELDS:
                    ls = score(line.get(lf))
                    if ls is not None:
                        scores.append(ls)

    if not scores:
        return 0

    overall = sum(scores, Decimal("0")) / Decimal(len(scores))
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
    # If LLM matching already verified with high confidence, no verification gating needed
    for key in ("overall_match_confidence", "overall_confidence"):
        if key in confidence:
            val = confidence[key]
            s = float(val.get("confidence", 0) if isinstance(val, dict) else val)
            if s >= confidence_threshold():
                return False
    threshold = confidence_threshold()
    present_fields = {
        field for field in CORE_EXTRACTION_FIELDS if field in confidence
    }
    # Require core fields: vendor, invoice_number, invoice_date, total
    if len(present_fields) < 2:
        return True
    return (
        any(
            entry["confidence"] < threshold
            for field, entry in confidence.items()
            if field in CORE_EXTRACTION_FIELDS and isinstance(entry, dict)
        )
        or any(
            entry[field]["confidence"] < threshold
            for entry in confidence.get("line_items", [])
            for field in LINE_CONFIDENCE_FIELDS
            if isinstance(entry, dict) and field in entry and isinstance(entry[field], dict)
        )
    )


def confidence_unavailable(confidence: dict[str, Any] | None) -> bool:
    if not confidence or any(
        field not in confidence for field in CORE_EXTRACTION_FIELDS
    ):
        return True
    if any(
        not isinstance(confidence.get(field), dict)
        or confidence[field].get("confidence") is None
        for field in CORE_EXTRACTION_FIELDS
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
