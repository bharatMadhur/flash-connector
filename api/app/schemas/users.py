from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class UserOut(BaseModel):
    id: str
    tenant_id: str
    email: str
    display_name: str | None
    role: str
    created_at: datetime
    last_login_at: datetime | None

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    email: str
    display_name: str | None = None
    role: Literal["owner", "admin", "dev", "viewer"] = "viewer"
    password: str


class UserUpdate(BaseModel):
    display_name: str | None = None
    role: Literal["owner", "admin", "dev", "viewer"] | None = None


class UserPasswordUpdate(BaseModel):
    password: str
