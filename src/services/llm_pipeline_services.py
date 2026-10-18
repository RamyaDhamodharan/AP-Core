
import json
import logging
import os
from typing import Any
from decimal import Decimal, InvalidOperation
import re
import httpx

logger = logging.getLogger(__name__)

# API endpoints
OPENROUTER_DECISIONS_URL = (
    "https://openrouter.ai/api/alpha/decisions"
)
GROQ_API_URL = (
    "https://api.groq.com/openai/v1/chat/completions"
)

DOCUMENT_TYPES = {"invoice", "po", "other"}


def classify_document(raw_text: str) -> str:
    """Classify document text using OpenRouter Luna Decisions."""
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    model = os.getenv("OPENROUTER_MODEL", "")

    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is not configured")

    if not model:
        raise ValueError("OPENROUTER_MODEL is not configured")

    payload = {
        "model": model,
        "state": raw_text[:20000],
        "questions": {
            "doc_type": {
                "type": "choice",
                "instructions": (
                    "Classify the document as invoice, po, or other. "
                    "Use invoice for a supplier invoice or bill, "
                    "po for a purchase order, and other for all "
                    "remaining document types."
                ),
                "criteria": {
                    "invoice": "Supplier invoice or bill",
                    "po": "Purchase order",
                    "other": "Any other document",
                },
            }
        },
    }

    response = httpx.post(
        OPENROUTER_DECISIONS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60.0,
    )
    response.raise_for_status()

    data = response.json()
    answers = data.get("answers", {})
    answer = answers.get("doc_type", {}) if isinstance(
        answers, dict
    ) else {}

    doc_type = (
        answer.get("choice")
        if isinstance(answer, dict)
        else None
    )

    if doc_type not in DOCUMENT_TYPES:
        raise ValueError(
            f"Luna returned an invalid document type: {doc_type!r}"
        )

    return doc_type


def _groq_extract(
    raw_text: str,
    doc_type: str,
    api_key: str,
    model: str,
) -> dict[str, Any]:
    """Extract structured document fields using a Groq model."""
    response = httpx.post(
        GROQ_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"Extract structured fields from this "
                        f"{doc_type} document. Return only a valid "
                        "JSON object. Do not invent values; use null "
                        "for missing values. Include document_type, "
                        "vendor, invoice_number, order_number, "
                        "invoice_date, due_date, currency, subtotal, "
                        "tax, total, and line_items when applicable. "
                        "Preserve values as they appear in the source."
                    ),
                },
                {
                    "role": "user",
                    "content": raw_text[:30000],
                },
            ],
        },
        timeout=60.0,
    )
    response.raise_for_status()

    data = response.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(
            "Groq returned an unexpected response structure"
        ) from exc

    if not isinstance(content, str) or not content.strip():
        raise ValueError("Groq returned empty extraction content")

    content = content.strip()

    # Remove optional Markdown code fences.
    if content.startswith("```"):
        lines = content.splitlines()

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        if lines and lines[0].strip().lower() in {
            "```json", "json"
        }:
            lines = lines[1:]
        elif lines and lines[0].strip().startswith("```"):
            lines = lines[1:]

        content = "\n".join(lines).strip()

    try:
        result: Any = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Groq returned invalid JSON extraction content"
        ) from exc

    if not isinstance(result, dict):
        raise ValueError("Extractor did not return a JSON object")

    return result


def extract_document(
    raw_text: str,
    doc_type: str,
) -> dict[str, Any]:
    """Extract document fields with primary and fallback Groq models."""
    api_key = os.getenv("GROQ_API_KEY", "")
    primary_model = os.getenv("GROQ_MODEL", "")
    fallback_model = os.getenv("GROQ_FALLBACK_MODEL", "")

    if not api_key:
        raise ValueError("GROQ_API_KEY is not configured")

    if not primary_model:
        raise ValueError("GROQ_MODEL is not configured")

    try:
        return _groq_extract(
            raw_text,
            doc_type,
            api_key,
            primary_model,
        )
    except Exception:
        logger.warning(
            "Primary extraction failed using model %s",
            primary_model,
            exc_info=True,
        )

        if not fallback_model or fallback_model == primary_model:
            raise

    logger.info(
        "Retrying extraction with fallback model %s",
        fallback_model,
    )

    return _groq_extract(
        raw_text,
        doc_type,
        api_key,
        fallback_model,
    )
def _to_decimal(value: Any) -> Decimal | None:
    """Convert monetary values with currency codes and symbols to Decimal."""
    if value is None or isinstance(value, bool):
        return None

    try:
        if isinstance(value, (int, float, Decimal)):
            result = Decimal(str(value))
            return result if result.is_finite() else None

        if not isinstance(value, str) or not value.strip():
            return None

        cleaned = value.strip()
        cleaned = re.sub(
            r"(?i)\b(?:INR|USD|EUR|GBP|RS\.?|RUPEES?)\b",
            "",
            cleaned,
        )
        cleaned = re.sub(r"[?$??,\s]", "", cleaned)

        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]

        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", cleaned):
            return None

        result = Decimal(cleaned)
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None

def validate_extracted_data(
    data: dict[str, Any],
    doc_type: str,
) -> list[str]:
    """Return review issues without modifying extracted values."""
    issues: list[str] = []

    if not isinstance(data, dict):
        return ["Extracted data must be a JSON object"]

    if doc_type not in DOCUMENT_TYPES:
        return [f"Unsupported document type: {doc_type}"]

    # Validate the document type returned by the extractor, if present.
    extracted_type = data.get("document_type")
    if extracted_type is not None:
        normalized_type = str(extracted_type).strip().lower()
        aliases = {"purchase_order": "po", "purchase order": "po"}

        if aliases.get(normalized_type, normalized_type) != doc_type:
            issues.append(
                f"Document type mismatch: expected {doc_type}"
            )

    if doc_type == "invoice":
        required_fields = (
            "vendor",
            "invoice_number",
            "invoice_date",
            "total",
        )
    elif doc_type == "po":
        required_fields = ("vendor", "order_number", "total")
    else:
        required_fields = ()

    for field in required_fields:
        value = data.get(field)
        if value is None or (
            isinstance(value, str) and not value.strip()
        ):
            issues.append(f"Missing required field: {field}")

    amounts = {
        field: _to_decimal(data.get(field))
        for field in ("subtotal", "tax", "total")
    }

    for field, amount in amounts.items():
        value = data.get(field)
        if value is not None and amount is None:
            issues.append(f"Invalid monetary value: {field}")

    subtotal = amounts["subtotal"]
    tax = amounts["tax"]
    total = amounts["total"]

    # Compare amounts only when all three values are available.
    # Allow a one-cent rounding difference.
    if subtotal is not None and tax is not None and total is not None:
        if abs(subtotal + tax - total) > Decimal("0.01"):
            issues.append(
                "Amount mismatch: subtotal plus tax does not equal total"
            )

    line_items = data.get("line_items")
    if line_items is not None:
        if not isinstance(line_items, list):
            issues.append("line_items must be a list")
        else:
            for index, item in enumerate(line_items):
                if not isinstance(item, dict):
                    issues.append(
                        f"Line item {index + 1} must be an object"
                    )

    return issues

def test_validate_extracted_data_accepts_valid_invoice():
    data = {
        "document_type": "invoice",
        "vendor": "ABC Supplies",
        "invoice_number": "INV-101",
        "invoice_date": "2026-10-01",
        "subtotal": 1500,
        "tax": 0,
        "total": 1500,
        "line_items": [],
    }

    assert validate_extracted_data(data, "invoice") == []


def test_validate_extracted_data_detects_missing_invoice_number():
    data = {
        "vendor": "ABC Supplies",
        "invoice_date": "2026-10-01",
        "total": 1500,
    }

    issues = validate_extracted_data(data, "invoice")

    assert "Missing required field: invoice_number" in issues


def test_validate_extracted_data_detects_amount_mismatch():
    data = {
        "vendor": "ABC Supplies",
        "invoice_number": "INV-101",
        "invoice_date": "2026-10-01",
        "subtotal": 1000,
        "tax": 100,
        "total": 1200,
    }

    issues = validate_extracted_data(data, "invoice")

    assert "Amount mismatch: subtotal plus tax does not equal total" in issues


def test_validate_extracted_data_detects_invalid_total():
    data = {
        "vendor": "ABC Supplies",
        "invoice_number": "INV-101",
        "invoice_date": "2026-10-01",
        "total": "unknown",
    }

    issues = validate_extracted_data(data, "invoice")

    assert "Invalid monetary value: total" in issues


def test_validate_extracted_data_accepts_valid_purchase_order():
    data = {
        "document_type": "po",
        "vendor": "ABC Supplies",
        "order_number": "PO-101",
        "total": 2500,
    }

    assert validate_extracted_data(data, "po") == []


def test_validate_extracted_data_rejects_invalid_line_items():
    data = {
        "vendor": "ABC Supplies",
        "invoice_number": "INV-101",
        "invoice_date": "2026-10-01",
        "total": 1500,
        "line_items": "not-a-list",
    }

    issues = validate_extracted_data(data, "invoice")

    assert "line_items must be a list" in issues
