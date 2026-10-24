import pytest

from src.services.runtime_config_services import (
    currency_conversion_enabled,
    invoice_only_requires_approval,
    require_goods_receipt,
)


@pytest.mark.parametrize(("setting", "expected"), [("true", True), ("false", False)])
def test_require_goods_receipt_flag(setting, expected, monkeypatch):
    monkeypatch.setenv("REQUIRE_GOODS_RECEIPT", setting)
    assert require_goods_receipt() is expected


@pytest.mark.parametrize(("setting", "expected"), [("true", True), ("false", False)])
def test_currency_conversion_flag(setting, expected, monkeypatch):
    monkeypatch.setenv("CURRENCY_CONVERSION", setting)
    assert currency_conversion_enabled() is expected


@pytest.mark.parametrize(("setting", "expected"), [("true", True), ("false", False)])
def test_invoice_only_approval_flag(setting, expected, monkeypatch):
    monkeypatch.setenv("INVOICE_ONLY_REQUIRES_APPROVAL", setting)
    assert invoice_only_requires_approval() is expected
