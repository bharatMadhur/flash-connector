"""Typed SDK response models for flash-connector.

These dataclasses mirror the JSON payloads returned by public endpoints and
provide light convenience helpers (for example ``is_terminal`` flags).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

SaveMode = Literal["full", "redacted"]


def _parse_dt(value: str | None) -> datetime | None:
    """Parse API datetime strings into timezone-aware ``datetime`` objects."""
    if value is None:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


@dataclass(slots=True)
class JobSubmission:
    """Minimal acknowledgement payload returned by job submit endpoint."""

    job_id: str
    status: str

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "JobSubmission":
        """Build model from raw API JSON dictionary."""
        return cls(job_id=str(payload["job_id"]), status=str(payload["status"]))


@dataclass(slots=True)
class BatchSubmission:
    """Minimal acknowledgement payload returned by batch submit endpoint."""

    batch_id: str
    status: str
    provider_slug: str
    model_used: str
    total_jobs: int

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "BatchSubmission":
        """Build model from raw API JSON dictionary."""
        return cls(
            batch_id=str(payload["batch_id"]),
            status=str(payload["status"]),
            provider_slug=str(payload.get("provider_slug") or ""),
            model_used=str(payload.get("model_used") or ""),
            total_jobs=int(payload.get("total_jobs") or 0),
        )


@dataclass(slots=True)
class BatchCancellation:
    """Cancellation acknowledgement payload for one provider batch run."""

    batch_id: str
    status: str

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "BatchCancellation":
        """Build model from raw API JSON dictionary."""
        return cls(batch_id=str(payload["batch_id"]), status=str(payload["status"]))


@dataclass(slots=True)
class BatchDetail:
    """Full provider batch run record returned by ``GET /v1/batches/{id}``."""

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

    @property
    def is_terminal(self) -> bool:
        """Return True when batch reached final state."""
        return self.status in {"completed", "failed", "canceled"}

    @property
    def is_success(self) -> bool:
        """Return True when batch completed successfully."""
        return self.status == "completed"

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "BatchDetail":
        """Build model from raw API JSON dictionary."""
        return cls(
            id=str(payload["id"]),
            tenant_id=str(payload["tenant_id"]),
            endpoint_id=str(payload["endpoint_id"]),
            endpoint_version_id=str(payload["endpoint_version_id"]),
            provider_slug=str(payload.get("provider_slug") or ""),
            provider_config_id=payload.get("provider_config_id"),
            model_used=str(payload.get("model_used") or ""),
            status=str(payload.get("status") or "queued"),
            completion_window=str(payload.get("completion_window") or "24h"),
            provider_batch_id=payload.get("provider_batch_id"),
            input_file_id=payload.get("input_file_id"),
            output_file_id=payload.get("output_file_id"),
            error_file_id=payload.get("error_file_id"),
            request_json=dict(payload.get("request_json") or {}),
            result_json=payload.get("result_json"),
            error=payload.get("error"),
            total_jobs=int(payload.get("total_jobs") or 0),
            completed_jobs=int(payload.get("completed_jobs") or 0),
            failed_jobs=int(payload.get("failed_jobs") or 0),
            canceled_jobs=int(payload.get("canceled_jobs") or 0),
            cancel_requested=bool(payload.get("cancel_requested", False)),
            created_by_user_id=payload.get("created_by_user_id"),
            created_at=_parse_dt(payload["created_at"]),
            started_at=_parse_dt(payload.get("started_at")),
            finished_at=_parse_dt(payload.get("finished_at")),
            last_polled_at=_parse_dt(payload.get("last_polled_at")),
            next_poll_at=_parse_dt(payload.get("next_poll_at")),
            updated_at=_parse_dt(payload["updated_at"]),
        )


@dataclass(slots=True)
class JobCancellation:
    """Cancellation acknowledgement payload for one async job."""

    job_id: str
    status: str

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "JobCancellation":
        """Build model from raw API JSON dictionary."""
        return cls(job_id=str(payload["job_id"]), status=str(payload["status"]))


@dataclass(slots=True)
class JobDetail:
    """Full async job record returned by ``GET /v1/jobs/{id}``."""

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

    @property
    def is_terminal(self) -> bool:
        """Return True when job reached final state."""
        return self.status in {"completed", "failed", "canceled"}

    @property
    def is_success(self) -> bool:
        """Return True when job completed successfully."""
        return self.status == "completed"

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "JobDetail":
        """Build model from raw API JSON dictionary."""
        return cls(
            id=str(payload["id"]),
            tenant_id=str(payload["tenant_id"]),
            endpoint_id=str(payload["endpoint_id"]),
            endpoint_version_id=str(payload["endpoint_version_id"]),
            billing_mode=str(payload.get("billing_mode") or "byok"),
            reserved_cost_usd=float(payload.get("reserved_cost_usd") or 0.0),
            request_api_key_id=payload.get("request_api_key_id"),
            idempotency_key=payload.get("idempotency_key"),
            subtenant_code=payload.get("subtenant_code"),
            status=str(payload["status"]),
            request_json=dict(payload.get("request_json") or {}),
            request_hash=payload.get("request_hash"),
            cache_hit=bool(payload.get("cache_hit", False)),
            cached_from_job_id=payload.get("cached_from_job_id"),
            result_text=payload.get("result_text"),
            error=payload.get("error"),
            usage_json=payload.get("usage_json"),
            estimated_cost_usd=(
                float(payload["estimated_cost_usd"]) if payload.get("estimated_cost_usd") is not None else None
            ),
            provider_response_id=payload.get("provider_response_id"),
            provider_used=payload.get("provider_used"),
            model_used=payload.get("model_used"),
            created_at=_parse_dt(payload["created_at"]),
            started_at=_parse_dt(payload.get("started_at")),
            finished_at=_parse_dt(payload.get("finished_at")),
        )


@dataclass(slots=True)
class TrainingEvent:
    """Training datastore event record returned by ``/save`` operations."""

    id: str
    tenant_id: str
    endpoint_id: str
    endpoint_version_id: str
    subtenant_code: str | None
    job_id: str | None
    input_json: dict[str, Any]
    output_text: str
    feedback: str | None
    edited_ideal_output: str | None
    tags: list[str]
    is_few_shot: bool
    created_at: datetime
    save_mode: str

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "TrainingEvent":
        """Build model from raw API JSON dictionary."""
        return cls(
            id=str(payload["id"]),
            tenant_id=str(payload["tenant_id"]),
            endpoint_id=str(payload["endpoint_id"]),
            endpoint_version_id=str(payload["endpoint_version_id"]),
            subtenant_code=payload.get("subtenant_code"),
            job_id=payload.get("job_id"),
            input_json=dict(payload.get("input_json") or {}),
            output_text=str(payload.get("output_text") or ""),
            feedback=payload.get("feedback"),
            edited_ideal_output=payload.get("edited_ideal_output"),
            tags=list(payload.get("tags") or []),
            is_few_shot=bool(payload.get("is_few_shot", False)),
            created_at=_parse_dt(payload["created_at"]),
            save_mode=str(payload["save_mode"]),
        )
