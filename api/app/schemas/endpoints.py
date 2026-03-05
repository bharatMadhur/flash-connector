from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.provider_catalog import ensure_supported_provider_slug


class EndpointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class EndpointUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class EndpointOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None
    active_version_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EndpointVersionCreate(BaseModel):
    system_prompt: str
    input_template: str | None = None
    variable_schema_json: list[dict[str, Any]] = Field(default_factory=list)
    target_id: str | None = None
    provider: str = "openai"
    model: str = "gpt-5-nano"
    params_json: dict[str, Any] = Field(default_factory=dict)
    persona_id: str | None = None
    context_block_ids: list[str] = Field(default_factory=list)

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        return ensure_supported_provider_slug(value)


class EndpointVersionOut(BaseModel):
    id: str
    endpoint_id: str
    version: int
    system_prompt: str
    input_template: str | None
    variable_schema_json: list[dict[str, Any]]
    target_id: str | None
    provider: str
    model: str
    params_json: dict[str, Any]
    persona_id: str | None
    created_at: datetime
    created_by_user_id: str | None

    model_config = {"from_attributes": True}


class ActivateVersionRequest(BaseModel):
    version_id: str


class PromptUpdateRequest(BaseModel):
    system_prompt: str
    input_template: str | None = None
    variable_schema_json: list[dict[str, Any]] = Field(default_factory=list)
    target_id: str | None = None
    provider: str = "openai"
    model: str = "gpt-5-nano"
    params_json: dict[str, Any] = Field(default_factory=dict)
    persona_id: str | None = None
    context_block_ids: list[str] = Field(default_factory=list)

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        return ensure_supported_provider_slug(value)
