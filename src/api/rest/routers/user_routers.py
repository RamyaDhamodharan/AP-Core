"""
Application module for user routers.
Flow: HTTP request handling before service calls and response formatting.
Called by: the framework, CLI, or application entry point.
Calls: dependencies, session_db, user_models.
Tables: users.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.user_models import User

router = APIRouter(prefix="/api/users", tags=["Users"])


@router.get("/assignable")
def list_assignable_users(
    db: Session = Depends(get_db), _user: dict = Depends(get_current_user)
):
    """
    List list assignable users.
    Inputs: db (Session), _user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows.
    Results: no workflow status or reason code directly.
    """
    users = (
        db.query(User)
        .filter(User.role == "associate", User.is_active.is_(True))
        .order_by(User.name, User.id)
        .all()
    )
    return {
        "items": [
            {"id": user.id, "name": user.name, "email": user.email} for user in users
        ]
    }
