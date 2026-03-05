from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


PortalPermission = Literal["view_jobs", "add_feedback", "edit_ideal_output", "export_training"]


class PortalLinkCreate(BaseModel):
    subtenant_code: str = Field(min_length=1, max_length=128)
    expires_at: datetime
    permissions: list[PortalPermission] = Field(default_factory=list)


class PortalLinkOut(BaseModel):
    id: str
    tenant_id: str
    subtenant_code: str
    token_prefix: str
    permissions_json: dict
    expires_at: datetime
    is_revoked: bool
    created_by_user_id: str | None
    created_at: datetime
    last_used_at: datetime | None

    model_config = {"from_attributes": True}


class PortalLinkCreateOut(BaseModel):
    link: PortalLinkOut
    access_url: str
