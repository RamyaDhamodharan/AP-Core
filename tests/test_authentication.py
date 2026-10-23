import uuid
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException

from src.api.rest.dependencies import get_current_user


def _credentials(subject, token_type="access"):
    payload = {"sub": subject, "type": token_type}
    token = jwt.encode(
        payload, "test-secret-for-authentication-tests", algorithm="HS256"
    )
    return SimpleNamespace(credentials=token)


def test_current_user_accepts_uuid_subject(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-authentication-tests")
    user_id = uuid.uuid4()

    db = SimpleNamespace(
        get=lambda *_args: SimpleNamespace(
            role="associate", is_active=True, must_change_password=False
        )
    )
    payload = get_current_user(_credentials(str(user_id)), db)

    assert payload["sub"] == str(user_id)
    assert payload["role"] == "associate"


def test_current_user_blocks_temporary_password_tokens(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-authentication-tests")
    user_id = uuid.uuid4()
    token = jwt.encode(
        {"sub": str(user_id), "type": "access", "must_change_password": True},
        "test-secret-for-authentication-tests",
        algorithm="HS256",
    )
    credentials = SimpleNamespace(credentials=token)

    with pytest.raises(HTTPException) as error:
        get_current_user(credentials)

    assert error.value.status_code == 403
    assert "Change your temporary password" in error.value.detail


@pytest.mark.parametrize(
    ("is_active", "must_change_password", "expected_detail"),
    [
        (False, False, "Account is inactive"),
        (True, True, "Change your temporary password before using the workspace"),
    ],
)
def test_current_user_enforces_live_account_state(
    monkeypatch, is_active, must_change_password, expected_detail
):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-authentication-tests")
    user_id = uuid.uuid4()
    db = SimpleNamespace(
        get=lambda *_args: SimpleNamespace(
            role="associate",
            is_active=is_active,
            must_change_password=must_change_password,
        )
    )

    with pytest.raises(HTTPException) as error:
        get_current_user(_credentials(str(user_id)), db)

    assert error.value.status_code == 403
    assert error.value.detail == expected_detail


@pytest.mark.parametrize("subject", [None, "", "not-a-uuid"])
def test_current_user_rejects_missing_or_malformed_uuid_subject(monkeypatch, subject):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-authentication-tests")

    with pytest.raises(HTTPException) as error:
        get_current_user(_credentials(subject))

    assert error.value.status_code == 401
    assert error.value.detail in {
        "Invalid access token",
        "Invalid or expired access token",
        "Authenticated user ID is invalid",
    }
