import os
import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError

bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict[str, object]:
    token = credentials.credentials

    secret = os.getenv("JWT_SECRET_KEY")
    algorithm = os.getenv("JWT_ALGORITHM", "HS256")

    if not secret or secret == "CHANGE_ME_TO_A_LONG_RANDOM_SECRET":
        raise RuntimeError("JWT_SECRET_KEY is not configured correctly")

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
        )

        subject = payload.get("sub")
        if payload.get("type") != "access" or not subject:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid access token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            payload["sub"] = str(uuid.UUID(str(subject)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authenticated user ID is invalid",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        return payload

    except InvalidTokenError as e:
        print("JWT VALIDATION ERROR:", repr(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


def require_role(role: str):
    def role_dependency(current_user: dict[str, object] = Depends(get_current_user)):
        if current_user.get("role") != role:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return current_user

    return role_dependency
