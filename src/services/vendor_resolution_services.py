"""
Application module for vendor resolution services.
Flow: shared business-flow service called by API routes and workers.
Called by: src/api/rest/routers/document_routers.py,
src/api/rest/routers/document_workflow_routers.py,
src/api/rest/routers/invoice_routers.py.
Calls: vendor_models.
Tables: vendor_aliases, vendors.
"""
import re
import uuid
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.vendor_models import Vendor, VendorAlias


def normalize_vendor_name(value: object) -> str:
    """
    Normalize normalize vendor name.
    Inputs: value (object).
    Output: str.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    if value is None:
        return ""
    normalized = re.sub(r"[^a-z0-9\s]", " ", str(value).casefold())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    suffix_pattern = re.compile(r"\s+(pvt|private|ltd|limited|llp|inc|co)\.?$")
    while suffix_pattern.search(normalized):
        normalized = suffix_pattern.sub("", normalized).strip()
    return normalized


def _normalized_name_expression(column: Any) -> Any:
    return func.lower(func.regexp_replace(func.trim(column), r"\s+", " ", "g"))


def find_canonical_name_conflict(
    db: Session,
    name: str,
    *,
    exclude_vendor_id: uuid.UUID | None = None,
) -> Vendor | None:
    """
    Find find canonical name conflict.
    Inputs: db (Session), name (str), exclude_vendor_id (uuid.UUID | None).
    Output: Vendor | None.
    Effects: reads database rows.
    Results: no workflow status or reason code directly.
    """
    normalized_name = normalize_vendor_name(name)
    if not normalized_name:
        return None
    filters = [_normalized_name_expression(Vendor.name) == normalized_name]
    if exclude_vendor_id is not None:
        filters.append(Vendor.id != exclude_vendor_id)
    return db.query(Vendor).filter(*filters).first()


def find_active_alias_conflict(
    db: Session,
    name: str,
    *,
    exclude_vendor_id: uuid.UUID | None = None,
) -> VendorAlias | None:
    """
    Find find active alias conflict.
    Inputs: db (Session), name (str), exclude_vendor_id (uuid.UUID | None).
    Output: VendorAlias | None.
    Effects: reads database rows.
    Results: no workflow status or reason code directly.
    """
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


def resolve_vendor(
    db: Session,
    name: object,
    *,
    tax_id: object = None,
) -> Vendor | None:
    """
    Resolve resolve vendor.
    Inputs: db (Session), name (object), tax_id (object).
    Output: Vendor | None.
    Effects: reads database rows.
    Results: no workflow status or reason code directly.
    """
    normalized_tax_id = re.sub(r"[^a-z0-9]", "", str(tax_id or "").casefold())
    if normalized_tax_id:
        matches = (
            db.query(Vendor)
            .filter(
                Vendor.is_active.is_(True),
                func.lower(func.regexp_replace(Vendor.tax_id, r"[^[:alnum:]]", "", "g"))
                == normalized_tax_id,
            )
            .all()
        )
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
        return None

    normalized_name = normalize_vendor_name(name)
    if not normalized_name:
        return None

    # Query aliases first so an explicitly configured alias wins over a
    # canonical name collision.  Both queries are scoped to active vendors;
    # vendor visibility is intentionally global for authenticated users.
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
    if len(vendor_ids) == 1:
        return alias_matches[0][1]
    if len(vendor_ids) > 1:
        return None

    canonical_rows = db.query(Vendor).filter(Vendor.is_active.is_(True)).all()
    # Some callers provide an already-filtered lightweight row (for example,
    # the workflow unit seam).  Preserve that contract while real ORM rows
    # still receive the full punctuation/suffix normalization below.
    canonical_vendors = [
        vendor
        for vendor in canonical_rows
        if not hasattr(vendor, "name")
        or normalize_vendor_name(vendor.name) == normalized_name
    ]
    return canonical_vendors[0] if len(canonical_vendors) == 1 else None


def suggest_vendor_candidates(
    db: Session,
    name: object,
    *,
    threshold: float = 0.90,
) -> list[Vendor]:
    """
    Return high-similarity active vendors without linking them.
    Inputs: db (Session), name (object), threshold (float).
    Output: list[Vendor].
    Effects: reads database rows.
    Results: no workflow status or reason code directly.
    """
    normalized = normalize_vendor_name(name)
    if not normalized:
        return []
    candidates: list[tuple[float, Vendor]] = []
    for vendor in db.query(Vendor).filter(Vendor.is_active.is_(True)).all():
        score = SequenceMatcher(
            None, normalized, normalize_vendor_name(vendor.name)
        ).ratio()
        if score >= threshold:
            candidates.append((score, vendor))
    return [
        vendor
        for _, vendor in sorted(candidates, key=lambda item: item[0], reverse=True)
    ]
