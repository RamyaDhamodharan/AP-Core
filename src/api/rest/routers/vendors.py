
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.db.session import get_db
from src.models.vendor import Vendor
from src.schemas.vendor import VendorCreate, VendorResponse

router = APIRouter(prefix="/api/vendors", tags=["Vendors"])


@router.get("", response_model=list[VendorResponse])
def list_vendors(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.scalars(
        select(Vendor).order_by(Vendor.name)
    ).all()


@router.post("", response_model=VendorResponse, status_code=201)
def create_vendor(
    payload: VendorCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    vendor = Vendor(
        name=payload.name.strip(),
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
    db.commit()
    db.refresh(vendor)

    return vendor
