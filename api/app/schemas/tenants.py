from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    name: str
    parent_tenant_id: str | None = None
    can_create_subtenants: bool = False
    inherit_provider_configs: bool = True
    query_params_mode: Literal["inherit", "merge", "override"] = "override"
    query_params_json: dict[str, Any] = Field(default_factory=dict)


class TenantUpdate(BaseModel):
    name: str | None = None
    can_create_subtenants: bool | None = None
    inherit_provider_configs: bool | None = None
    query_params_mode: Literal["inherit", "merge", "override"] | None = None
    query_params_json: dict[str, Any] | None = None


class TenantOut(BaseModel):
    id: str
    name: str
    parent_tenant_id: str | None
    can_create_subtenants: bool
    inherit_provider_configs: bool
    query_params_mode: str
    query_params_json: dict[str, Any]
    llm_auth_mode: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TenantLLMSettingsUpdate(BaseModel):
    llm_auth_mode: Literal["platform", "tenant"]
    openai_api_key: str | None = None
    clear_tenant_key: bool = False


class TenantLLMSettingsOut(BaseModel):
    tenant_id: str
    llm_auth_mode: str
    has_tenant_key: bool
    has_platform_key: bool
