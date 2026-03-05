from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


class JobCreateRequest(BaseModel):
    input: str | None = None
    messages: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    subtenant_code: str | None = None
    save_default: bool = False

    @model_validator(mode="after")
    def validate_payload(self) -> "JobCreateRequest":
        if not self.input and not self.messages:
            raise ValueError("Either input or messages must be provided")
        return self


class JobCreateResponse(BaseModel):
    job_id: str
    status: str


class JobOut(BaseModel):
    id: str
    tenant_id: str
    endpoint_id: str
    endpoint_version_id: str
    billing_mode: str
    reserved_cost_usd: float
    request_api_key_id: str | None
    idempotency_key: str | None
    subtenant_code: str | None
    status: str
    request_json: dict[str, Any]
    request_hash: str | None
    cache_hit: bool
    cached_from_job_id: str | None
    result_text: str | None
    error: str | None
    usage_json: dict[str, Any] | None
    estimated_cost_usd: float | None
    provider_response_id: str | None
    provider_used: str | None
    model_used: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class JobCancelResponse(BaseModel):
    job_id: str
    status: str
