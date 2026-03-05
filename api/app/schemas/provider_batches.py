from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ProviderBatchItemRequest(BaseModel):
    input: str | None = None
    messages: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    subtenant_code: str | None = None
    save_default: bool = False

    @model_validator(mode="after")
    def validate_item(self) -> "ProviderBatchItemRequest":
        if not self.input and not self.messages:
            raise ValueError("Each batch item must include input or messages")
        return self


class ProviderBatchCreateRequest(BaseModel):
    items: list[ProviderBatchItemRequest] = Field(default_factory=list)
    batch_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    subtenant_code: str | None = None
    save_default: bool = False
    service_tier: Literal["auto", "default", "flex", "priority"] = "auto"
    completion_window: Literal["24h"] = "24h"

    @model_validator(mode="after")
    def validate_payload(self) -> "ProviderBatchCreateRequest":
        if not self.items:
            raise ValueError("At least one batch item is required")
        if len(self.items) > 1000:
            raise ValueError("Batch supports up to 1000 items per request")
        return self


class ProviderBatchCreateResponse(BaseModel):
    batch_id: str
    status: str
    provider_slug: str
    model_used: str
    total_jobs: int


class ProviderBatchCancelResponse(BaseModel):
    batch_id: str
    status: str


class ProviderBatchOut(BaseModel):
    id: str
    tenant_id: str
    endpoint_id: str
    endpoint_version_id: str
    provider_slug: str
    provider_config_id: str | None
    model_used: str
    status: str
    completion_window: str
    provider_batch_id: str | None
    input_file_id: str | None
    output_file_id: str | None
    error_file_id: str | None
    request_json: dict[str, Any]
    result_json: dict[str, Any] | None
    error: str | None
    total_jobs: int
    completed_jobs: int
    failed_jobs: int
    canceled_jobs: int
    cancel_requested: bool
    created_by_user_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    last_polled_at: datetime | None
    next_poll_at: datetime | None
    updated_at: datetime

    model_config = {"from_attributes": True}
