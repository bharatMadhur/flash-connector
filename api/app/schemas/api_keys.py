from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class ApiKeyScopes(BaseModel):
    all: bool = True
    endpoint_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_scope_shape(self) -> "ApiKeyScopes":
        if self.all:
            self.endpoint_ids = []
            return self
        cleaned = [item.strip() for item in self.endpoint_ids if isinstance(item, str) and item.strip()]
        deduped = sorted(set(cleaned))
        if not deduped:
            raise ValueError("endpoint_ids must be provided when scopes.all is false")
        self.endpoint_ids = deduped
        return self


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    scopes: ApiKeyScopes = Field(default_factory=ApiKeyScopes)
    rate_limit_per_min: int = Field(default=60, ge=1, le=5000)
    monthly_quota: int = Field(default=10000, ge=1)


class ApiKeyOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    key_prefix: str
    scopes: ApiKeyScopes
    rate_limit_per_min: int
    monthly_quota: int
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None

    model_config = {"from_attributes": True}


class ApiKeyCreateOut(BaseModel):
    id: str
    name: str
    key_prefix: str
    key: str
