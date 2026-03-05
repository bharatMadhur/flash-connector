from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobStatus
from app.services.pricing import extract_token_usage


@dataclass(frozen=True)
class UsageBucket:
    key: str
    label: str
    jobs_total: int
    jobs_completed: int
    jobs_failed: int
    jobs_canceled: int
    estimated_cost_usd: float
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class UsageSummary:
    window_hours: int
    from_at: datetime
    to_at: datetime
    jobs_total: int
    jobs_completed: int
    jobs_failed: int
    jobs_canceled: int
    estimated_cost_usd: float
    byok_cost_usd: float
    input_tokens: int
    output_tokens: int
    total_tokens: int
    by_billing_mode: list[UsageBucket]
    by_subtenant: list[UsageBucket]
    by_provider: list[UsageBucket]


@dataclass
class _MutableBucket:
    key: str
    label: str
    jobs_total: int = 0
    jobs_completed: int = 0
    jobs_failed: int = 0
    jobs_canceled: int = 0
    estimated_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def freeze(self) -> UsageBucket:
        return UsageBucket(
            key=self.key,
            label=self.label,
            jobs_total=self.jobs_total,
            jobs_completed=self.jobs_completed,
            jobs_failed=self.jobs_failed,
            jobs_canceled=self.jobs_canceled,
            estimated_cost_usd=self.estimated_cost_usd,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
        )


def _bounded_int(value: int, *, min_value: int, max_value: int) -> int:
    return min(max(int(value), min_value), max_value)


def _nonnegative_float(value: Any) -> float:
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _get_bucket(buckets: dict[str, _MutableBucket], key: str, label: str) -> _MutableBucket:
    bucket = buckets.get(key)
    if bucket is None:
        bucket = _MutableBucket(key=key, label=label)
        buckets[key] = bucket
    return bucket


def _add_job_to_bucket(bucket: _MutableBucket, job: Job, *, include_tokens: bool) -> None:
    bucket.jobs_total += 1
    if job.status == JobStatus.completed:
        bucket.jobs_completed += 1
    elif job.status == JobStatus.failed:
        bucket.jobs_failed += 1
    elif job.status == JobStatus.canceled:
        bucket.jobs_canceled += 1

    bucket.estimated_cost_usd += _nonnegative_float(job.estimated_cost_usd)

    if include_tokens and isinstance(job.usage_json, dict):
        tokens = extract_token_usage(job.usage_json)
        bucket.input_tokens += tokens.input_tokens
        bucket.output_tokens += tokens.output_tokens


def _sorted_buckets(
    buckets: dict[str, _MutableBucket],
    *,
    limit: int,
    custom_order: list[str] | None = None,
) -> list[UsageBucket]:
    if not buckets:
        return []

    frozen = [bucket.freeze() for bucket in buckets.values()]
    if custom_order:
        rank = {key: index for index, key in enumerate(custom_order)}
        frozen.sort(
            key=lambda item: (
                rank.get(item.key, len(rank) + 1),
                -item.estimated_cost_usd,
                -item.jobs_total,
                -item.total_tokens,
            )
        )
        return frozen[:limit]

    frozen.sort(key=lambda item: (item.estimated_cost_usd, item.jobs_total, item.total_tokens), reverse=True)
    return frozen[:limit]


def build_usage_summary(
    db: Session,
    *,
    tenant_id: str,
    window_hours: int = 24,
    bucket_limit: int = 12,
) -> UsageSummary:
    bounded_window_hours = _bounded_int(window_hours, min_value=1, max_value=24 * 365)
    bounded_bucket_limit = _bounded_int(bucket_limit, min_value=1, max_value=100)

    to_at = datetime.now(UTC)
    from_at = to_at - timedelta(hours=bounded_window_hours)

    jobs = db.scalars(
        select(Job).where(
            Job.tenant_id == tenant_id,
            Job.created_at >= from_at,
            Job.created_at <= to_at,
        )
    ).all()

    jobs_total = 0
    jobs_completed = 0
    jobs_failed = 0
    jobs_canceled = 0
    estimated_cost_usd = 0.0
    byok_cost_usd = 0.0
    input_tokens = 0
    output_tokens = 0

    billing_buckets: dict[str, _MutableBucket] = {}
    subtenant_buckets: dict[str, _MutableBucket] = {}
    provider_buckets: dict[str, _MutableBucket] = {}

    for job in jobs:
        jobs_total += 1
        if job.status == JobStatus.completed:
            jobs_completed += 1
        elif job.status == JobStatus.failed:
            jobs_failed += 1
        elif job.status == JobStatus.canceled:
            jobs_canceled += 1

        cost = _nonnegative_float(job.estimated_cost_usd)
        estimated_cost_usd += cost

        byok_cost_usd += cost
        billing_key = "byok"
        billing_label = "BYOK"

        if isinstance(job.usage_json, dict):
            tokens = extract_token_usage(job.usage_json)
            input_tokens += tokens.input_tokens
            output_tokens += tokens.output_tokens

        _add_job_to_bucket(
            _get_bucket(billing_buckets, billing_key, billing_label),
            job,
            include_tokens=True,
        )

        subtenant_code = (job.subtenant_code or "").strip()
        subtenant_key = subtenant_code if subtenant_code else "__none__"
        subtenant_label = subtenant_code if subtenant_code else "(none)"
        _add_job_to_bucket(
            _get_bucket(subtenant_buckets, subtenant_key, subtenant_label),
            job,
            include_tokens=True,
        )

        provider_code = (job.provider_used or "").strip()
        provider_key = provider_code if provider_code else "unknown"
        provider_label = provider_code if provider_code else "(unknown)"
        _add_job_to_bucket(
            _get_bucket(provider_buckets, provider_key, provider_label),
            job,
            include_tokens=True,
        )

    return UsageSummary(
        window_hours=bounded_window_hours,
        from_at=from_at,
        to_at=to_at,
        jobs_total=jobs_total,
        jobs_completed=jobs_completed,
        jobs_failed=jobs_failed,
        jobs_canceled=jobs_canceled,
        estimated_cost_usd=estimated_cost_usd,
        byok_cost_usd=byok_cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        by_billing_mode=_sorted_buckets(
            billing_buckets,
            limit=bounded_bucket_limit,
            custom_order=["byok"],
        ),
        by_subtenant=_sorted_buckets(subtenant_buckets, limit=bounded_bucket_limit),
        by_provider=_sorted_buckets(provider_buckets, limit=bounded_bucket_limit),
    )
