"""Read-only audit log search API."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.audit_models import AuditLog

router = APIRouter(prefix="/api/audit-log", tags=["Audit"])


@router.get("")
def search_audit_log(
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    action: str | None = None,
    result: str | None = None,
    user: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    query = db.query(AuditLog)
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if entity_id:
        query = query.filter(AuditLog.entity_id == entity_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if result:
        query = query.filter(AuditLog.result == result)
    if user:
        query = query.filter(AuditLog.created_by == user)
    if date_from:
        query = query.filter(AuditLog.created_at >= date_from)
    if date_to:
        query = query.filter(AuditLog.created_at <= date_to)
    total = query.count()
    ordering = (
        AuditLog.created_at.asc() if sort == "asc" else AuditLog.created_at.desc()
    )
    items = (
        query.order_by(ordering, AuditLog.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    safe_items = [
        {
            "id": item.id,
            "action": item.action,
            "entity_type": item.entity_type,
            "entity_id": item.entity_id,
            "old_value": item.old_value,
            "new_value": item.new_value,
            "result": item.result,
            "message": item.message,
            "step_name": item.step_name,
            "duration_ms": item.duration_ms,
            "input_ref": item.input_ref,
            "output_summary": item.output_summary,
            "created_by": item.created_by,
            "created_at": item.created_at,
        }
        for item in items
    ]
    return {"items": safe_items, "total": total, "page": page, "page_size": page_size}
