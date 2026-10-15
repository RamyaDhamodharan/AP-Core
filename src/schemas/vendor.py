
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class VendorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    tax_id: str | None = None
    is_active: bool
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None




class VendorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tax_id: str | None = Field(default=None, max_length=30)
    contact_name: str | None = Field(default=None, max_length=120)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=40)
