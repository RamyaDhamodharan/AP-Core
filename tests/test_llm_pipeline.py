
import httpx
import pytest

from src.services.llm_pipeline import (
    classify_document,
    extract_document,
    validate_extracted_data,
)


def test_classify_document_returns_invoice(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv(
        "OPENROUTER_MODEL",
        "openai/gpt-6-luna-decisions",
    )

    def mock_post(*args, **kwargs):
        assert kwargs["json"]["state"]
        return httpx.Response(
            200,
            json={
                "answers": {
                    "doc_type": {
                        "choice": "invoice",
                    }
                }
            },
            request=httpx.Request("POST", "https://example.com"),
        )

    monkeypatch.setattr(
        "src.services.llm_pipeline.httpx.post",
        mock_post,
    )

    result = classify_document("Supplier invoice INV-101")

    assert result == "invoice"


def test_classify_document_rejects_invalid_type(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "test-model")

    def mock_post(*args, **kwargs):
        return httpx.Response(
            200,
            json={
                "answers": {
                    "doc_type": {
                        "choice": "contract",
                    }
                }
            },
            request=httpx.Request("POST", "https://example.com"),
        )

    monkeypatch.setattr(
        "src.services.llm_pipeline.httpx.post",
        mock_post,
    )

    with pytest.raises(ValueError, match="invalid document type"):
        classify_document("A contract document")


def test_extract_document_returns_json(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_MODEL", "test-primary")
    monkeypatch.setenv("GROQ_FALLBACK_MODEL", "test-fallback")

    def mock_post(*args, **kwargs):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"invoice_number": "INV-101", '
                                '"total": 1500}'
                            )
                        }
                    }
                ]
            },
            request=httpx.Request("POST", "https://example.com"),
        )

    monkeypatch.setattr(
        "src.services.llm_pipeline.httpx.post",
        mock_post,
    )

    result = extract_document(
        "Invoice INV-101. Total: 1500",
        "invoice",
    )

    assert result["invoice_number"] == "INV-101"
    assert result["total"] == 1500

def test_extract_document_uses_fallback_model(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_MODEL", "test-primary")
    monkeypatch.setenv("GROQ_FALLBACK_MODEL", "test-fallback")

    attempted_models = []

    def mock_post(*args, **kwargs):
        model = kwargs["json"]["model"]
        attempted_models.append(model)

        if model == "test-primary":
            raise httpx.HTTPError("Primary model failed")

        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"invoice_number": "INV-202"}'
                        }
                    }
                ]
            },
            request=httpx.Request("POST", "https://example.com"),
        )

    monkeypatch.setattr(
        "src.services.llm_pipeline.httpx.post",
        mock_post,
    )

    result = extract_document("Invoice INV-202", "invoice")

    assert attempted_models == ["test-primary", "test-fallback"]
    assert result["invoice_number"] == "INV-202"

def test_extract_document_raises_when_both_models_fail(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_MODEL", "test-primary")
    monkeypatch.setenv("GROQ_FALLBACK_MODEL", "test-fallback")

    attempted_models = []

    def mock_post(*args, **kwargs):
        attempted_models.append(kwargs["json"]["model"])
        raise httpx.HTTPError("Model unavailable")

    monkeypatch.setattr(
        "src.services.llm_pipeline.httpx.post",
        mock_post,
    )

    with pytest.raises(httpx.HTTPError):
        extract_document("Supplier invoice INV-101", "invoice")

    assert attempted_models == ["test-primary", "test-fallback"]



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
