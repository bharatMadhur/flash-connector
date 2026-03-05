from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.core.provider_catalog import ensure_supported_provider_slug


class ProviderCatalogOut(BaseModel):
    slug: str
    name: str
    logo_path: str | None = None
    model_prefix: str
    default_model: str
    recommended_models: list[str]
    platform_key_env: str | None
    requires_api_key: bool
    docs_url: str
    realtime_docs_url: str | None


class ProviderConfigCreate(BaseModel):
    provider_slug: str = Field(min_length=1, max_length=64)
    provider_config_id: str | None = None
    connection_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_default: bool | None = None
    billing_mode: Literal["byok"] | None = None
    auth_mode: Literal["platform", "tenant", "none"] = "platform"
    api_key: str | None = None
    clear_api_key: bool = False
    api_base: str | None = Field(default=None, max_length=255)
    api_version: str | None = Field(default=None, max_length=64)
    extra_json: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True

    @field_validator("provider_slug")
    @classmethod
    def validate_provider_slug(cls, value: str) -> str:
        return ensure_supported_provider_slug(value.strip())


class ProviderConfigUpdate(BaseModel):
    connection_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_default: bool | None = None
    billing_mode: Literal["byok"] | None = None
    auth_mode: Literal["platform", "tenant", "none"] | None = None
    api_key: str | None = None
    clear_api_key: bool = False
    api_base: str | None = Field(default=None, max_length=255)
    api_version: str | None = Field(default=None, max_length=64)
    extra_json: dict[str, Any] | None = None
    is_active: bool | None = None


class ProviderConfigOut(BaseModel):
    id: str
    tenant_id: str
    provider_slug: str
    provider_name: str
    connection_name: str
    description: str | None
    is_default: bool
    model_prefix: str
    billing_mode: str
    auth_mode: str
    has_tenant_key: bool
    platform_key_available: bool
    api_base: str | None
    api_version: str | None
    extra_json: dict[str, Any]
    is_active: bool
    created_at: datetime
    updated_at: datetime
