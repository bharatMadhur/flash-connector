"""Public data-plane API.

Clients authenticate with virtual keys (`x-api-key`) and use async submit/poll
contracts for jobs and provider-native batches.
"""

from fastapi import APIRouter, Depends, HTTPException, Header, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typing import Any
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.db import get_db
from app.dependencies import (
    SessionUser,
    get_api_key_context,
    get_optional_api_key_context,
    get_optional_session_user,
)
from app.models import JobStatus
from app.schemas.jobs import JobCancelResponse, JobCreateRequest, JobCreateResponse, JobOut
from app.schemas.provider_batches import (
    ProviderBatchCancelResponse,
    ProviderBatchCreateRequest,
    ProviderBatchCreateResponse,
    ProviderBatchOut,
)
from app.schemas.training import SaveTrainingRequest, TrainingEventOut
from app.services.api_keys import ApiKeyContext, key_allows_endpoint
from app.services.provider_batches import (
    create_provider_batch_run,
    get_provider_batch_for_tenant,
    request_cancel_provider_batch_run,
)
from app.services.jobs import (
    cancel_job,
    create_job,
    get_active_version,
    get_idempotent_job_for_key,
    get_job_for_tenant,
    get_tenant_endpoint,
)
from app.services.queue import get_queue
from app.services.rate_limit import enforce_limits
from app.services.training import create_training_event_from_job
from app.tasks import process_job

router = APIRouter(tags=["public-api"])


def _queue_batch_poll_if_due(db: Session, batch: ProviderBatchOut | Any) -> None:
    status_value = str(getattr(batch, "status", "") or "").lower()
    if status_value in {"completed", "failed", "canceled"}:
        return
    next_poll_at = getattr(batch, "next_poll_at", None)
    now = datetime.now(UTC)
    if next_poll_at is not None and next_poll_at > now:
        return
    queue = get_queue()
    settings = get_settings()
    queue.enqueue("app.tasks.poll_provider_batch_run", batch.id, 0, job_timeout=settings.job_timeout_seconds)
    if db is not None and hasattr(batch, "next_poll_at"):
        batch.next_poll_at = now + timedelta(seconds=settings.provider_batch_poll_interval_seconds)
        db.add(batch)
        db.commit()


@router.post("/v1/endpoints/{endpoint_id}/jobs", response_model=JobCreateResponse)
def submit_job(
    endpoint_id: str,
    payload: JobCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    api_ctx: ApiKeyContext = Depends(get_api_key_context),
) -> JobCreateResponse:
    if not key_allows_endpoint(api_ctx.scopes, endpoint_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key scope does not allow this endpoint")

    allowed, reason = enforce_limits(api_ctx.api_key_id, api_ctx.rate_limit_per_min, api_ctx.monthly_quota)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason)

    endpoint = get_tenant_endpoint(db, api_ctx.tenant_id, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    active_version = get_active_version(db, endpoint)
    if active_version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Endpoint has no active version")

    normalized_idempotency_key = (idempotency_key or "").strip() or None
    if normalized_idempotency_key is not None and len(normalized_idempotency_key) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must be 128 characters or fewer",
        )

    if normalized_idempotency_key is not None:
        existing = get_idempotent_job_for_key(
            db,
            tenant_id=api_ctx.tenant_id,
            endpoint_id=endpoint.id,
            request_api_key_id=api_ctx.api_key_id,
            idempotency_key=normalized_idempotency_key,
        )
        if existing is not None:
            return JobCreateResponse(job_id=existing.id, status=existing.status.value)

    try:
        job = create_job(
            db,
            tenant_id=api_ctx.tenant_id,
            endpoint=endpoint,
            active_version=active_version,
            request_payload=payload,
            request_api_key_id=api_ctx.api_key_id,
            idempotency_key=normalized_idempotency_key,
            billing_mode="byok",
            reserved_cost_usd=0.0,
        )
    except IntegrityError:
        db.rollback()
        if normalized_idempotency_key is None:
            raise
        existing = get_idempotent_job_for_key(
            db,
            tenant_id=api_ctx.tenant_id,
            endpoint_id=endpoint.id,
            request_api_key_id=api_ctx.api_key_id,
            idempotency_key=normalized_idempotency_key,
        )
        if existing is None:
            raise
        return JobCreateResponse(job_id=existing.id, status=existing.status.value)

    queue = get_queue()
    settings = get_settings()
    queue.enqueue("app.tasks.process_job", job.id, job_id=job.id, job_timeout=settings.job_timeout_seconds)

    return JobCreateResponse(job_id=job.id, status=job.status.value)


@router.post("/v1/endpoints/{endpoint_id}/responses", response_model=JobOut)
def submit_response(
    endpoint_id: str,
    payload: JobCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    api_ctx: ApiKeyContext = Depends(get_api_key_context),
) -> JobOut:
    """Execute a request inline and return full job payload immediately."""
    if not key_allows_endpoint(api_ctx.scopes, endpoint_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key scope does not allow this endpoint")

    allowed, reason = enforce_limits(api_ctx.api_key_id, api_ctx.rate_limit_per_min, api_ctx.monthly_quota)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason)

    endpoint = get_tenant_endpoint(db, api_ctx.tenant_id, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    active_version = get_active_version(db, endpoint)
    if active_version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Endpoint has no active version")

    normalized_idempotency_key = (idempotency_key or "").strip() or None
    if normalized_idempotency_key is not None and len(normalized_idempotency_key) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must be 128 characters or fewer",
        )

    job = None
    if normalized_idempotency_key is not None:
        job = get_idempotent_job_for_key(
            db,
            tenant_id=api_ctx.tenant_id,
            endpoint_id=endpoint.id,
            request_api_key_id=api_ctx.api_key_id,
            idempotency_key=normalized_idempotency_key,
        )
        if job is not None and job.status == JobStatus.running:
            return JobOut.model_validate(job)
        if job is not None and job.status == JobStatus.queued:
            try:
                process_job(job.id)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Inline execution failed: {exc}",
                ) from exc
            db.expire_all()
            latest = get_job_for_tenant(db, api_ctx.tenant_id, job.id)
            if latest is None:
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Job not found after execution")
            return JobOut.model_validate(latest)
        if job is not None:
            return JobOut.model_validate(job)

    try:
        job = create_job(
            db,
            tenant_id=api_ctx.tenant_id,
            endpoint=endpoint,
            active_version=active_version,
            request_payload=payload,
            request_api_key_id=api_ctx.api_key_id,
            idempotency_key=normalized_idempotency_key,
            billing_mode="byok",
            reserved_cost_usd=0.0,
        )
    except IntegrityError:
        db.rollback()
        if normalized_idempotency_key is None:
            raise
        existing = get_idempotent_job_for_key(
            db,
            tenant_id=api_ctx.tenant_id,
            endpoint_id=endpoint.id,
            request_api_key_id=api_ctx.api_key_id,
            idempotency_key=normalized_idempotency_key,
        )
        if existing is None:
            raise
        job = existing

    try:
        process_job(job.id)
    except Exception as exc:  # noqa: BLE001
        db.expire_all()
        latest = get_job_for_tenant(db, api_ctx.tenant_id, job.id)
        if latest is not None and latest.status in {JobStatus.queued, JobStatus.running}:
            latest.status = JobStatus.failed
            latest.error = f"Inline execution failed: {exc}"
            latest.finished_at = datetime.now(UTC)
            db.add(latest)
            db.commit()
            db.refresh(latest)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Inline execution failed: {exc}",
        ) from exc

    db.expire_all()
    latest = get_job_for_tenant(db, api_ctx.tenant_id, job.id)
    if latest is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Job not found after execution")
    return JobOut.model_validate(latest)


@router.post("/v1/endpoints/{endpoint_id}/batches", response_model=ProviderBatchCreateResponse)
def submit_batch(
    endpoint_id: str,
    payload: ProviderBatchCreateRequest,
    db: Session = Depends(get_db),
    api_ctx: ApiKeyContext = Depends(get_api_key_context),
) -> ProviderBatchCreateResponse:
    if not key_allows_endpoint(api_ctx.scopes, endpoint_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key scope does not allow this endpoint")

    allowed, reason = enforce_limits(api_ctx.api_key_id, api_ctx.rate_limit_per_min, api_ctx.monthly_quota)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason)

    endpoint = get_tenant_endpoint(db, api_ctx.tenant_id, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    active_version = get_active_version(db, endpoint)
    if active_version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Endpoint has no active version")

    try:
        run = create_provider_batch_run(
            db,
            tenant_id=api_ctx.tenant_id,
            endpoint=endpoint,
            active_version=active_version,
            payload=payload,
            request_api_key_id=api_ctx.api_key_id,
            created_by_user_id=None,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    queue = get_queue()
    settings = get_settings()
    queue.enqueue("app.tasks.submit_provider_batch_run", run.id, job_timeout=max(600, settings.job_timeout_seconds))

    return ProviderBatchCreateResponse(
        batch_id=run.id,
        status=run.status,
        provider_slug=run.provider_slug,
        model_used=run.model_used,
        total_jobs=run.total_jobs,
    )


@router.get("/v1/batches/{batch_id}", response_model=ProviderBatchOut)
def get_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    api_ctx: ApiKeyContext = Depends(get_api_key_context),
) -> ProviderBatchOut:
    run = get_provider_batch_for_tenant(db, api_ctx.tenant_id, batch_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    if not key_allows_endpoint(api_ctx.scopes, run.endpoint_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key scope does not allow this batch")

    _queue_batch_poll_if_due(db, run)
    return ProviderBatchOut.model_validate(run)


@router.post("/v1/batches/{batch_id}/cancel", response_model=ProviderBatchCancelResponse)
def cancel_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    api_ctx: ApiKeyContext = Depends(get_api_key_context),
) -> ProviderBatchCancelResponse:
    run = get_provider_batch_for_tenant(db, api_ctx.tenant_id, batch_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    if not key_allows_endpoint(api_ctx.scopes, run.endpoint_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key scope does not allow this batch")

    updated = request_cancel_provider_batch_run(db, run)
    if updated.provider_batch_id and updated.status not in {"completed", "failed", "canceled"}:
        queue = get_queue()
        settings = get_settings()
        queue.enqueue("app.tasks.poll_provider_batch_run", updated.id, 0, job_timeout=settings.job_timeout_seconds)
    return ProviderBatchCancelResponse(batch_id=updated.id, status=updated.status)


@router.get("/v1/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    api_ctx: ApiKeyContext = Depends(get_api_key_context),
) -> JobOut:
    job = get_job_for_tenant(db, api_ctx.tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if not key_allows_endpoint(api_ctx.scopes, job.endpoint_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key scope does not allow this job")

    return JobOut.model_validate(job)


@router.post("/v1/jobs/{job_id}/cancel", response_model=JobCancelResponse)
def cancel_public_job(
    job_id: str,
    db: Session = Depends(get_db),
    api_ctx: ApiKeyContext = Depends(get_api_key_context),
) -> JobCancelResponse:
    job = get_job_for_tenant(db, api_ctx.tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if not key_allows_endpoint(api_ctx.scopes, job.endpoint_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key scope does not allow this job")

    canceled = cancel_job(job, db)
    return JobCancelResponse(job_id=canceled.id, status=canceled.status.value)


@router.post("/v1/jobs/{job_id}/save", response_model=TrainingEventOut)
def save_job_training(
    job_id: str,
    payload: SaveTrainingRequest,
    db: Session = Depends(get_db),
    api_ctx: ApiKeyContext | None = Depends(get_optional_api_key_context),
    session_user: SessionUser | None = Depends(get_optional_session_user),
) -> TrainingEventOut:
    tenant_id: str | None = None
    if api_ctx is not None and session_user is not None and api_ctx.tenant_id != session_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant mismatch")
    if api_ctx is not None:
        tenant_id = api_ctx.tenant_id
    if session_user is not None:
        tenant_id = session_user.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    job = get_job_for_tenant(db, tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if api_ctx is not None and not key_allows_endpoint(api_ctx.scopes, job.endpoint_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key scope does not allow this job")

    event = create_training_event_from_job(db, tenant_id=tenant_id, job=job, payload=payload)
    return TrainingEventOut.model_validate(event)
