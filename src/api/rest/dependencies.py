import os

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError

bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
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

        if payload.get("type") != "access" or not payload.get("sub"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid access token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return payload

    except InvalidTokenError as e:
        print("JWT VALIDATION ERROR:", repr(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
