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

    payload = get_current_user(_credentials(str(user_id)))

    assert payload["sub"] == str(user_id)


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
