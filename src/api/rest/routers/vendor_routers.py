import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.vendor_models import Vendor
from src.schemas.vendor_schemas import (
    VendorCreate,
    VendorResponse,
    VendorUpdate,
)

router = APIRouter(prefix="/api/vendors", tags=["Vendors"])


def _duplicate_vendor(
    db: Session,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> Vendor | None:
    filters = [
        func.lower(func.trim(Vendor.name)) == name.casefold(),
    ]
    if exclude_id is not None:
        filters.append(Vendor.id != exclude_id)
    return db.scalars(select(Vendor).where(*filters)).first()


def _raise_vendor_conflict(exc: IntegrityError) -> None:
    raise HTTPException(
        status_code=409,
        detail="Vendor conflicts with existing data",
    ) from exc


@router.get(
    "",
    response_model=list[VendorResponse],
    dependencies=[Depends(get_current_user)],
)
def list_vendors(
    search: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int | None, Query(ge=1, le=100)] = None,
    db: Session = Depends(get_db),
) -> list[Vendor]:
    statement = select(Vendor)
    if search is not None:
        search = search.strip()
        if not search:
            raise HTTPException(
                status_code=422,
                detail="search must not be blank",
            )
        pattern = f"%{search}%"
        statement = statement.where(
            or_(
                Vendor.name.ilike(pattern),
                Vendor.tax_id.ilike(pattern),
                Vendor.contact_name.ilike(pattern),
                Vendor.contact_email.ilike(pattern),
                Vendor.contact_phone.ilike(pattern),
            )
        )

    statement = statement.order_by(Vendor.name)
    if page is not None or page_size is not None:
        resolved_page = page or 1
        resolved_page_size = page_size or 20
        statement = statement.offset(
            (resolved_page - 1) * resolved_page_size
        ).limit(resolved_page_size)

    return db.scalars(statement).all()


@router.get(
    "/{vendor_id}",
    response_model=VendorResponse,
    dependencies=[Depends(get_current_user)],
)
def get_vendor(
    vendor_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Vendor:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@router.post(
    "",
    response_model=VendorResponse,
    status_code=201,
    dependencies=[Depends(get_current_user)],
)
def create_vendor(
    payload: VendorCreate,
    db: Session = Depends(get_db),
) -> Vendor:
    if _duplicate_vendor(db, payload.name):
        raise HTTPException(
            status_code=409,
            detail="A vendor with this name already exists",
        )

    vendor = Vendor(
        name=payload.name,
        tax_id=payload.tax_id,
        contact_name=payload.contact_name,
        contact_email=(
            str(payload.contact_email)
            if payload.contact_email
            else None
        ),
        contact_phone=payload.contact_phone,
    )

    db.add(vendor)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_vendor_conflict(exc)
    db.refresh(vendor)

    return vendor


@router.patch(
    "/{vendor_id}",
    response_model=VendorResponse,
    dependencies=[Depends(get_current_user)],
)
def update_vendor(
    vendor_id: uuid.UUID,
    payload: VendorUpdate,
    db: Session = Depends(get_db),
) -> Vendor:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=422,
            detail="At least one vendor field must be provided",
        )

    if "name" in updates and _duplicate_vendor(
        db,
        updates["name"],
        exclude_id=vendor.id,
    ):
        raise HTTPException(
            status_code=409,
            detail="A vendor with this name already exists",
        )

    if "contact_email" in updates and updates["contact_email"] is not None:
        updates["contact_email"] = str(updates["contact_email"])

    for field, value in updates.items():
        setattr(vendor, field, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_vendor_conflict(exc)
    db.refresh(vendor)
    return vendor
