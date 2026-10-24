import pytest

from src.services.runtime_config_services import no_match_auto_reject


@pytest.mark.parametrize(("setting", "expected"), [("true", True), ("false", False)])
def test_no_match_auto_reject_flag(setting, expected, monkeypatch):
    monkeypatch.setenv("NO_MATCH_AUTO_REJECT", setting)
    monkeypatch.delenv("EMAIL_NO_MATCH_AUTO_REJECT", raising=False)
    assert no_match_auto_reject() is expected


@pytest.mark.parametrize(("setting", "expected"), [("true", True), ("false", False)])
def test_no_match_auto_reject_legacy_alias(setting, expected, monkeypatch):
    monkeypatch.delenv("NO_MATCH_AUTO_REJECT", raising=False)
    monkeypatch.setenv("EMAIL_NO_MATCH_AUTO_REJECT", setting)
    assert no_match_auto_reject() is expected
