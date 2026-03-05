from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PersonaCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    instructions: str = Field(min_length=1)
    style_json: dict[str, Any] = Field(default_factory=dict)


class PersonaUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    instructions: str | None = Field(default=None, min_length=1)
    style_json: dict[str, Any] | None = None


class PersonaOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None
    instructions: str
    style_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContextBlockCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class ContextBlockUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1)
    tags: list[str] | None = None


class ContextBlockOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    content: str
    tags: list[str] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TenantVariableCreate(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: str
    is_secret: bool = False


class TenantVariableUpdate(BaseModel):
    value: str | None = None
    is_secret: bool | None = None


class TenantVariableOut(BaseModel):
    id: str
    tenant_id: str
    key: str
    value: str
    is_secret: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
