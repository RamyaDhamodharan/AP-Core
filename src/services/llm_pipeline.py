
import json
import logging
import os
from typing import Any

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
