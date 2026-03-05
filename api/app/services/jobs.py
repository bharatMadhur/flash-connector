"""Job lifecycle helpers for public submit/poll APIs."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.security import generate_job_id
from app.models import Endpoint, EndpointVersion, Job, JobStatus
from app.schemas.jobs import JobCreateRequest
from app.services.queue import cancel_enqueued_job
from app.services.tenants import resolve_effective_query_params



def create_job(
    db: Session,
    *,
    tenant_id: str,
    endpoint: Endpoint,
    active_version: EndpointVersion,
    request_payload: JobCreateRequest,
    request_api_key_id: str | None = None,
    provider_batch_run_id: str | None = None,
    provider_batch_item_id: str | None = None,
    provider_batch_status: str | None = None,
    idempotency_key: str | None = None,
    billing_mode: str = "byok",
    reserved_cost_usd: float = 0.0,
    auto_commit: bool = True,
) -> Job:
    """Create queued job row with tenant metadata/query-param attribution."""
    effective_query_params = resolve_effective_query_params(db, tenant_id)
    metadata_subtenant = (request_payload.metadata or {}).get("subtenant_code")
    subtenant_code = (request_payload.subtenant_code or "").strip() or None
    if subtenant_code is None and isinstance(metadata_subtenant, str):
        subtenant_code = metadata_subtenant.strip() or None
    merged_metadata = {
        **effective_query_params,
        **(request_payload.metadata or {}),
    }
    if subtenant_code and "subtenant_code" not in merged_metadata:
        merged_metadata["subtenant_code"] = subtenant_code

    job = Job(
        id=generate_job_id(),
        tenant_id=tenant_id,
        endpoint_id=endpoint.id,
        endpoint_version_id=active_version.id,
        billing_mode=billing_mode,
        reserved_cost_usd=max(float(reserved_cost_usd), 0.0),
        request_api_key_id=request_api_key_id,
        provider_batch_run_id=provider_batch_run_id,
        provider_batch_item_id=provider_batch_item_id,
        provider_batch_status=provider_batch_status,
        idempotency_key=idempotency_key,
        subtenant_code=subtenant_code,
        status=JobStatus.queued,
        request_json={
            "input": request_payload.input,
            "messages": request_payload.messages,
            "metadata": merged_metadata,
            "effective_query_params": effective_query_params,
            "subtenant_code": subtenant_code,
            "idempotency_key": idempotency_key,
            "save_default": request_payload.save_default,
        },
    )
    db.add(job)
    if auto_commit:
        db.commit()
        db.refresh(job)
    else:
        db.flush()
    return job



def get_tenant_endpoint(db: Session, tenant_id: str, endpoint_id: str) -> Endpoint | None:
    """Return endpoint owned by tenant or ``None``."""
    return db.scalar(select(Endpoint).where(and_(Endpoint.id == endpoint_id, Endpoint.tenant_id == tenant_id)))



def get_active_version(db: Session, endpoint: Endpoint) -> EndpointVersion | None:
    """Return endpoint's currently active version if configured."""
    if not endpoint.active_version_id:
        return None
    return db.scalar(
        select(EndpointVersion).where(
            EndpointVersion.id == endpoint.active_version_id, EndpointVersion.endpoint_id == endpoint.id
        )
    )



def get_job_for_tenant(db: Session, tenant_id: str, job_id: str) -> Job | None:
    """Return one job scoped to tenant id."""
    return db.scalar(select(Job).where(and_(Job.id == job_id, Job.tenant_id == tenant_id)))


def get_idempotent_job_for_key(
    db: Session,
    *,
    tenant_id: str,
    endpoint_id: str,
    request_api_key_id: str,
    idempotency_key: str,
) -> Job | None:
    """Return previously submitted job for same endpoint/key/idempotency tuple."""
    return db.scalar(
        select(Job).where(
            and_(
                Job.tenant_id == tenant_id,
                Job.endpoint_id == endpoint_id,
                Job.request_api_key_id == request_api_key_id,
                Job.idempotency_key == idempotency_key,
            )
        )
    )



def cancel_job(job: Job, db: Session) -> Job:
    """Cancel queued job immediately or mark running job for deferred cancel."""
    now = datetime.now(UTC)
    if job.status == JobStatus.queued:
        cancel_enqueued_job(job.id)
        job.status = JobStatus.canceled
        job.finished_at = now
    elif job.status == JobStatus.running:
        job.cancel_requested = True
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
