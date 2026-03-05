from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.core.provider_catalog import ensure_supported_provider_slug


CapabilityProfile = Literal["responses_chat"]


class TargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    provider_config_id: str = Field(min_length=1, max_length=36)
    provider_slug: str = Field(min_length=1, max_length=64)
    capability_profile: CapabilityProfile = "responses_chat"
    model_identifier: str = Field(min_length=1, max_length=128)
    params_json: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True

    @field_validator("provider_slug")
    @classmethod
    def validate_provider_slug(cls, value: str) -> str:
        return ensure_supported_provider_slug(value.strip())


class TargetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    provider_config_id: str | None = Field(default=None, min_length=1, max_length=36)
    provider_slug: str | None = Field(default=None, min_length=1, max_length=64)
    capability_profile: CapabilityProfile | None = None
    model_identifier: str | None = Field(default=None, min_length=1, max_length=128)
    params_json: dict[str, Any] | None = None
    is_active: bool | None = None

    @field_validator("provider_slug")
    @classmethod
    def validate_provider_slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return ensure_supported_provider_slug(value.strip())


class TargetOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    provider_config_id: str | None
    provider_slug: str
    capability_profile: CapabilityProfile
    model_identifier: str
    params_json: dict[str, Any]
    is_active: bool
    is_verified: bool
    last_verified_at: datetime | None
    last_verification_error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TargetVerifyResponse(BaseModel):
    ok: bool
    message: str
    target: TargetOut
