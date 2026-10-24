import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user, require_role
from src.db.session_db import get_db
from src.models.vendor_models import Vendor, VendorAlias
from src.schemas.vendor_schemas import (
    VendorAliasCreate,
    VendorAliasResponse,
    VendorAliasUpdate,
    VendorCreate,
    VendorResponse,
    VendorUpdate,
)
from src.services.vendor_resolution_services import (
    find_active_alias_conflict,
    find_canonical_name_conflict,
    normalize_vendor_name,
)

router = APIRouter(prefix="/api/vendors", tags=["Vendors"])


def _duplicate_vendor(
    db: Session,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> Vendor | None:
    return find_canonical_name_conflict(
        db,
        name,
        exclude_vendor_id=exclude_id,
    )


def _ensure_name_not_another_vendors_alias(
    db: Session,
    name: str,
    *,
    vendor_id: uuid.UUID | None = None,
) -> None:
    if find_active_alias_conflict(
        db,
        name,
        exclude_vendor_id=vendor_id,
    ):
        raise HTTPException(
            status_code=409,
            detail="Vendor name conflicts with an active vendor alias",
        )


def _get_active_vendor_or_conflict(
    db: Session,
    vendor_id: uuid.UUID,
) -> Vendor:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if not vendor.is_active:
        raise HTTPException(
            status_code=409,
            detail="Aliases can only be assigned to active vendors",
        )
    return vendor


def _validate_alias_mapping(
    db: Session,
    alias: str,
    vendor_id: uuid.UUID,
    *,
    exclude_alias_id: uuid.UUID | None = None,
    active: bool = True,
) -> None:
    if active and find_canonical_name_conflict(
        db,
        alias,
        exclude_vendor_id=vendor_id,
    ):
        raise HTTPException(
            status_code=409,
            detail="Alias conflicts with another vendor's canonical name",
        )

    query = db.query(VendorAlias).filter(
        VendorAlias.alias_norm == normalize_vendor_name(alias)
    )
    if exclude_alias_id is not None:
        query = query.filter(VendorAlias.id != exclude_alias_id)
    if query.first():
        raise HTTPException(
            status_code=409,
            detail="A vendor alias with this normalized name already exists",
        )


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
        statement = statement.offset((resolved_page - 1) * resolved_page_size).limit(
            resolved_page_size
        )

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
    dependencies=[Depends(require_role("admin"))],
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
    _ensure_name_not_another_vendors_alias(db, payload.name)

    vendor = Vendor(
        name=payload.name,
        tax_id=payload.tax_id,
        default_tax_rate=payload.default_tax_rate,
        contact_name=payload.contact_name,
        contact_email=(str(payload.contact_email) if payload.contact_email else None),
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
    dependencies=[Depends(require_role("admin"))],
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
    if "name" in updates:
        _ensure_name_not_another_vendors_alias(
            db,
            updates["name"],
            vendor_id=vendor.id,
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


@router.delete(
    "/{vendor_id}",
    response_model=VendorResponse,
    dependencies=[Depends(require_role("admin"))],
)
def deactivate_vendor(
    vendor_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Vendor:
    """Deactivate a vendor without breaking historical record references."""
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    vendor.is_active = False
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


@router.get(
    "/{vendor_id}/aliases",
    response_model=list[VendorAliasResponse],
    dependencies=[Depends(get_current_user)],
)
def list_vendor_aliases(
    vendor_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> list[VendorAlias]:
    if db.get(Vendor, vendor_id) is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return (
        db.query(VendorAlias)
        .filter(VendorAlias.vendor_id == vendor_id)
        .order_by(VendorAlias.alias_norm)
        .all()
    )


@router.post(
    "/{vendor_id}/aliases",
    response_model=VendorAliasResponse,
    status_code=201,
    dependencies=[Depends(require_role("admin"))],
)
def create_vendor_alias(
    vendor_id: uuid.UUID,
    payload: VendorAliasCreate,
    db: Session = Depends(get_db),
) -> VendorAlias:
    _get_active_vendor_or_conflict(db, vendor_id)
    _validate_alias_mapping(db, payload.alias, vendor_id)

    alias = VendorAlias(
        vendor_id=vendor_id,
        alias_norm=payload.alias,
        is_active=True,
    )
    db.add(alias)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_vendor_conflict(exc)
    db.refresh(alias)
    return alias


@router.patch(
    "/{vendor_id}/aliases/{alias_id}",
    response_model=VendorAliasResponse,
    dependencies=[Depends(require_role("admin"))],
)
def update_vendor_alias(
    vendor_id: uuid.UUID,
    alias_id: uuid.UUID,
    payload: VendorAliasUpdate,
    db: Session = Depends(get_db),
) -> VendorAlias:
    alias = db.get(VendorAlias, alias_id)
    if alias is None or alias.vendor_id != vendor_id:
        raise HTTPException(status_code=404, detail="Vendor alias not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=422,
            detail="At least one alias field must be provided",
        )

    target_vendor_id = updates.get("vendor_id", alias.vendor_id)
    target_vendor = _get_active_vendor_or_conflict(db, target_vendor_id)
    next_alias_name = updates.get("alias", alias.alias_norm)
    next_active = updates.get("is_active", alias.is_active)
    _validate_alias_mapping(
        db,
        next_alias_name,
        target_vendor.id,
        exclude_alias_id=alias.id,
        active=next_active,
    )

    alias.vendor_id = target_vendor.id
    alias.alias_norm = next_alias_name
    alias.is_active = next_active
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_vendor_conflict(exc)
    db.refresh(alias)
    return alias


@router.delete(
    "/{vendor_id}/aliases/{alias_id}",
    response_model=VendorAliasResponse,
    dependencies=[Depends(require_role("admin"))],
)
def deactivate_vendor_alias(
    vendor_id: uuid.UUID,
    alias_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> VendorAlias:
    alias = db.get(VendorAlias, alias_id)
    if alias is None or alias.vendor_id != vendor_id:
        raise HTTPException(status_code=404, detail="Vendor alias not found")
    alias.is_active = False
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_vendor_conflict(exc)
    db.refresh(alias)
    return alias
