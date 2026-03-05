from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.provider_catalog import ensure_supported_provider_slug
from app.services.pricing import normalize_model_pattern


class PricingRateCreate(BaseModel):
    provider_slug: str = Field(min_length=1, max_length=64)
    model_pattern: str = Field(default="*", min_length=1, max_length=128)
    input_per_1m_usd: float = Field(ge=0)
    output_per_1m_usd: float = Field(ge=0)
    cached_input_per_1m_usd: float | None = Field(default=None, ge=0)
    is_active: bool = True

    @field_validator("provider_slug")
    @classmethod
    def validate_provider_slug(cls, value: str) -> str:
        return ensure_supported_provider_slug(value.strip())

    @field_validator("model_pattern")
    @classmethod
    def validate_model_pattern(cls, value: str) -> str:
        return normalize_model_pattern(value)


class PricingRateUpdate(BaseModel):
    input_per_1m_usd: float | None = Field(default=None, ge=0)
    output_per_1m_usd: float | None = Field(default=None, ge=0)
    cached_input_per_1m_usd: float | None = Field(default=None, ge=0)
    is_active: bool | None = None


class PricingRateOut(BaseModel):
    id: str
    tenant_id: str
    provider_slug: str
    model_pattern: str
    input_per_1m_usd: float
    output_per_1m_usd: float
    cached_input_per_1m_usd: float | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BuiltinPricingRateOut(BaseModel):
    provider_slug: str
    model_pattern: str
    input_per_1m_usd: float
    output_per_1m_usd: float
    cached_input_per_1m_usd: float | None
    source: str = "builtin_estimate"
