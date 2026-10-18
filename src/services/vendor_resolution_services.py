import re
import uuid
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.vendor_models import Vendor, VendorAlias


def normalize_vendor_name(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def _normalized_name_expression(column: Any) -> Any:
    return func.lower(
        func.regexp_replace(func.trim(column), r"\s+", " ", "g")
    )


def find_canonical_name_conflict(
    db: Session,
    name: str,
    *,
    exclude_vendor_id: uuid.UUID | None = None,
) -> Vendor | None:
    normalized_name = normalize_vendor_name(name)
    if not normalized_name:
        return None
    filters = [
        _normalized_name_expression(Vendor.name) == normalized_name
    ]
    if exclude_vendor_id is not None:
        filters.append(Vendor.id != exclude_vendor_id)
    return db.query(Vendor).filter(*filters).first()


def find_active_alias_conflict(
    db: Session,
    name: str,
    *,
    exclude_vendor_id: uuid.UUID | None = None,
) -> VendorAlias | None:
    normalized_name = normalize_vendor_name(name)
    if not normalized_name:
        return None
    filters = [
        VendorAlias.alias_norm == normalized_name,
        VendorAlias.is_active.is_(True),
    ]
    if exclude_vendor_id is not None:
        filters.append(VendorAlias.vendor_id != exclude_vendor_id)
    return db.query(VendorAlias).filter(*filters).first()


def resolve_vendor(db: Session, name: object) -> Vendor | None:
    normalized_name = normalize_vendor_name(name)
    if not normalized_name:
        return None

    canonical_vendors = (
        db.query(Vendor)
        .filter(
            _normalized_name_expression(Vendor.name) == normalized_name,
            Vendor.is_active.is_(True),
        )
        .all()
    )
    if len(canonical_vendors) == 1:
        return canonical_vendors[0]
    if canonical_vendors:
        return None

    alias_matches = (
        db.query(VendorAlias, Vendor)
        .join(Vendor, VendorAlias.vendor_id == Vendor.id)
        .filter(
            VendorAlias.alias_norm == normalized_name,
            VendorAlias.is_active.is_(True),
            Vendor.is_active.is_(True),
        )
        .all()
    )
    vendor_ids = {vendor.id for _, vendor in alias_matches}
    if len(vendor_ids) != 1:
        return None
    return alias_matches[0][1]
