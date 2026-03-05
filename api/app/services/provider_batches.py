from __future__ import annotations

import io
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.provider_catalog import ensure_supported_provider_slug
from app.models import (
    Endpoint,
    EndpointVersion,
    Job,
    JobStatus,
    Persona,
    ProviderBatchRun,
    Target,
)
from app.schemas.jobs import JobCreateRequest
from app.schemas.provider_batches import (
    ProviderBatchCreateRequest,
)
from app.services.jobs import create_job
from app.services.llm import (
    build_provider_client,
    extract_response_text_from_dict,
    extract_usage_from_dict,
    sanitize_responses_params_for_model,
)
from app.services.pricing import estimate_job_cost_usd
from app.services.prompt_studio import (
    build_request_hash,
    collect_request_text,
    compose_system_prompt,
    find_blocked_phrase,
    list_context_blocks_for_version,
    parse_int_param,
    parse_list_param,
    render_job_input,
    tenant_variables_map,
)
from app.services.providers import (
    get_effective_provider_config,
    get_tenant_provider_config_by_id,
    resolve_provider_credentials,
)
from app.services.queue import get_queue
from app.services.token_advisor import build_token_cost_advisor
from app.services.training import auto_save_training_event, list_few_shot_examples

SUPPORTED_PROVIDER_BATCH_PROVIDERS = {
    "openai",
    "azure_openai",
    "azure_openai_v1",
    "azure_openai_deployment",
    "azure_ai_foundry",
}
TERMINAL_BATCH_STATUSES = {"completed", "failed", "canceled"}


def _generate_provider_batch_run_id() -> str:
    return f"pbr_{secrets.token_urlsafe(10).replace('-', '').replace('_', '')}"


def _batch_status_from_provider(provider_status: str | None) -> str:
    value = (provider_status or "").strip().lower()
    mapping = {
        "validating": "submitted",
        "queued": "submitted",
        "in_progress": "processing",
        "finalizing": "finalizing",
        "completed": "completed",
        "failed": "failed",
        "expired": "failed",
        "cancelling": "canceling",
        "cancelled": "canceled",
        "canceled": "canceled",
    }
    return mapping.get(value, "submitted")


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return dumped
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        dumped = to_dict()
        if isinstance(dumped, dict):
            return dumped
    return {}


def _parse_bool_param(params: dict[str, Any], key: str, *, default: bool = False) -> bool:
    raw = params.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
    return default


def _normalize_service_tier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    if cleaned in {"auto", "default", "flex", "priority"}:
        return cleaned
    return None


def _render_few_shot_block(examples: list[tuple[str, str]]) -> str:
    lines: list[str] = [
        "Few-shot examples:",
        "Use these examples to mirror style and structure. Do not copy them verbatim.",
    ]
    for index, (example_input, example_output) in enumerate(examples, start=1):
        lines.append(f"Example {index} user:")
        lines.append(example_input)
        lines.append(f"Example {index} assistant:")
        lines.append(example_output)
    return "\n".join(lines)


def _mark_job_failed(db: Session, job: Job, message: str, *, provider_batch_status: str = "failed") -> None:
    job.status = JobStatus.failed
    job.error = message
    job.estimated_cost_usd = None
    job.provider_batch_status = provider_batch_status
    job.finished_at = datetime.now(UTC)
    db.add(job)


def _mark_job_canceled(db: Session, job: Job) -> None:
    job.status = JobStatus.canceled
    job.provider_batch_status = "canceled"
    job.error = "Canceled"
    job.finished_at = datetime.now(UTC)
    db.add(job)


def _compute_batch_counters(db: Session, batch_id: str) -> tuple[int, int, int, int]:
    rows = db.execute(
        select(Job.status, func.count(Job.id))
        .where(Job.provider_batch_run_id == batch_id)
        .group_by(Job.status)
    ).all()

    total = 0
    completed = 0
    failed = 0
    canceled = 0
    for status, count in rows:
        count_int = int(count)
        total += count_int
        if status == JobStatus.completed:
            completed += count_int
        elif status == JobStatus.failed:
            failed += count_int
        elif status == JobStatus.canceled:
            canceled += count_int
    return total, completed, failed, canceled


def refresh_provider_batch_counts(db: Session, run: ProviderBatchRun) -> ProviderBatchRun:
    total, completed, failed, canceled = _compute_batch_counters(db, run.id)
    run.total_jobs = total
    run.completed_jobs = completed
    run.failed_jobs = failed
    run.canceled_jobs = canceled
    db.add(run)
    return run


def get_provider_batch_for_tenant(db: Session, tenant_id: str, batch_id: str) -> ProviderBatchRun | None:
    return db.scalar(
        select(ProviderBatchRun).where(
            ProviderBatchRun.id == batch_id,
            ProviderBatchRun.tenant_id == tenant_id,
        )
    )


def list_provider_batches_for_tenant(
    db: Session,
    tenant_id: str,
    *,
    limit: int = 100,
) -> list[ProviderBatchRun]:
    return db.scalars(
        select(ProviderBatchRun)
        .where(ProviderBatchRun.tenant_id == tenant_id)
        .order_by(ProviderBatchRun.created_at.desc())
        .limit(limit)
    ).all()


def list_jobs_for_provider_batch(db: Session, tenant_id: str, batch_id: str, *, limit: int = 5000) -> list[Job]:
    return db.scalars(
        select(Job)
        .where(
            Job.tenant_id == tenant_id,
            Job.provider_batch_run_id == batch_id,
        )
        .order_by(Job.created_at.asc())
        .limit(limit)
    ).all()


def _resolve_batch_runtime(
    db: Session,
    *,
    tenant_id: str,
    endpoint: Endpoint,
    active_version: EndpointVersion,
) -> tuple[str, str, str | None, str]:
    provider_slug = ensure_supported_provider_slug(active_version.provider or "openai")
    model_used = active_version.model
    provider_config_id: str | None = None

    selected_target = None
    if active_version.target_id:
        selected_target = db.scalar(
            select(Target).where(Target.id == active_version.target_id, Target.tenant_id == tenant_id)
        )

    if selected_target is not None:
        provider_slug = ensure_supported_provider_slug(selected_target.provider_slug)
        model_used = selected_target.model_identifier
        provider_config_id = selected_target.provider_config_id

    if provider_slug not in SUPPORTED_PROVIDER_BATCH_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_PROVIDER_BATCH_PROVIDERS))
        raise RuntimeError(f"Provider-native batch supports only: {supported}")

    credentials = resolve_provider_credentials(
        db,
        tenant_id=tenant_id,
        provider_slug=provider_slug,
        provider_config_id=provider_config_id,
    )
    if not credentials.api_key:
        raise RuntimeError(f"No API key available for provider '{provider_slug}'")

    return provider_slug, model_used, provider_config_id, credentials.provider_slug


def create_provider_batch_run(
    db: Session,
    *,
    tenant_id: str,
    endpoint: Endpoint,
    active_version: EndpointVersion,
    payload: ProviderBatchCreateRequest,
    request_api_key_id: str | None,
    created_by_user_id: str | None,
) -> ProviderBatchRun:
    provider_slug, model_used, provider_config_id, _ = _resolve_batch_runtime(
        db,
        tenant_id=tenant_id,
        endpoint=endpoint,
        active_version=active_version,
    )

    provider_config = None
    if provider_config_id:
        provider_config = get_tenant_provider_config_by_id(db, tenant_id, provider_config_id)
    if provider_config is None:
        provider_config, _ = get_effective_provider_config(
            db,
            tenant_id=tenant_id,
            provider_slug=provider_slug,
        )

    billing_mode = "byok"

    batch_name = (payload.batch_name or "").strip()
    effective_service_tier = _normalize_service_tier(payload.service_tier) or "auto"
    shared_metadata = {
        **(payload.metadata or {}),
        "service_tier": effective_service_tier,
    }
    run = ProviderBatchRun(
        id=_generate_provider_batch_run_id(),
        tenant_id=tenant_id,
        endpoint_id=endpoint.id,
        endpoint_version_id=active_version.id,
        provider_slug=provider_slug,
        provider_config_id=provider_config_id,
        model_used=model_used,
        status="queued",
        completion_window=payload.completion_window,
        request_json={
            "batch_name": batch_name or None,
            "metadata": shared_metadata,
            "subtenant_code": payload.subtenant_code,
            "save_default": payload.save_default,
            "service_tier": effective_service_tier,
        },
        total_jobs=len(payload.items),
        created_by_user_id=created_by_user_id,
    )
    db.add(run)
    db.flush()

    for index, item in enumerate(payload.items):
        merged_metadata = {
            **shared_metadata,
            **(item.metadata or {}),
            "provider_batch_run_id": run.id,
            "provider_batch_index": index + 1,
            "provider_batch_total": len(payload.items),
            "source": "provider_batch",
        }
        if batch_name:
            merged_metadata["provider_batch_name"] = batch_name

        request_payload = JobCreateRequest(
            input=item.input,
            messages=item.messages,
            metadata=merged_metadata,
            subtenant_code=(item.subtenant_code or payload.subtenant_code),
            save_default=bool(item.save_default or payload.save_default),
        )
        job = create_job(
            db,
            tenant_id=tenant_id,
            endpoint=endpoint,
            active_version=active_version,
            request_payload=request_payload,
            request_api_key_id=request_api_key_id,
            provider_batch_run_id=run.id,
            provider_batch_status="queued",
            billing_mode=billing_mode,
            reserved_cost_usd=0.0,
            auto_commit=False,
        )
        job.provider_batch_item_id = job.id
        db.add(job)

    db.commit()

    refresh_provider_batch_counts(db, run)
    db.commit()
    db.refresh(run)
    return run


def request_cancel_provider_batch_run(db: Session, run: ProviderBatchRun) -> ProviderBatchRun:
    if run.status in TERMINAL_BATCH_STATUSES:
        return run

    run.cancel_requested = True
    if run.status in {"queued", "failed"}:
        run.status = "canceled"
        run.finished_at = datetime.now(UTC)
        jobs = list_jobs_for_provider_batch(db, run.tenant_id, run.id)
        for job in jobs:
            if job.status == JobStatus.queued:
                _mark_job_canceled(db, job)
    else:
        run.status = "canceling"

    refresh_provider_batch_counts(db, run)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _prepare_batch_request_body(
    db: Session,
    *,
    run: ProviderBatchRun,
    version: EndpointVersion,
    job: Job,
    persona: Persona | None,
    context_blocks: list[Any],
) -> tuple[dict[str, Any] | None, str | None]:
    req = job.request_json or {}
    metadata = req.get("metadata") if isinstance(req.get("metadata"), dict) else {}
    input_text = req.get("input") if isinstance(req.get("input"), str) else None
    messages = req.get("messages") if isinstance(req.get("messages"), list) else None

    tenant_variables = tenant_variables_map(db, job.tenant_id)
    rendered_input, _ = render_job_input(
        input_template=version.input_template,
        input_text=input_text,
        metadata=metadata,
        tenant_variables=tenant_variables,
    )

    params = version.params_json or {}
    request_hash = build_request_hash(
        endpoint_version_id=version.id,
        input_text=rendered_input,
        messages=messages,
        metadata=metadata,
    )
    job.request_hash = request_hash
    if rendered_input is not None:
        job.request_json = {**req, "rendered_input": rendered_input}

    blocked_input_phrases = parse_list_param(params, "blocked_input_phrases")
    request_text = collect_request_text(rendered_input, messages)
    blocked_input = find_blocked_phrase(request_text, blocked_input_phrases)
    if blocked_input:
        return None, f"Input blocked by phrase: {blocked_input}"

    system_prompt = compose_system_prompt(
        system_prompt=version.system_prompt,
        persona=persona,
        context_blocks=context_blocks,
    )

    few_shot_enabled = _parse_bool_param(params, "few_shot_enabled", default=False)
    few_shot_limit = parse_int_param(params, "few_shot_limit", default=0, min_value=0, max_value=20)
    few_shot_count = 0
    if few_shot_enabled and few_shot_limit > 0:
        few_shot_examples = list_few_shot_examples(
            db,
            tenant_id=job.tenant_id,
            endpoint_id=job.endpoint_id,
            limit=few_shot_limit,
        )
        few_shot_count = len(few_shot_examples)
        if few_shot_examples:
            system_prompt = f"{system_prompt}\n\n{_render_few_shot_block(few_shot_examples)}"

    request_params, dropped_params = sanitize_responses_params_for_model(run.model_used, params)
    service_tier = _normalize_service_tier(metadata.get("service_tier"))
    if service_tier:
        request_params["service_tier"] = service_tier
    usage_payload = {
        "attempted_routes": [{"provider": run.provider_slug, "model": run.model_used}],
        "selected_provider": run.provider_slug,
        "selected_model": run.model_used,
        "cache_hit": False,
        "persona": persona.name if persona else None,
        "context_blocks": [block.name for block in context_blocks],
        "few_shot_enabled": few_shot_enabled,
        "few_shot_count": few_shot_count,
        "batch_mode": "provider_native",
    }
    usage_payload["advisor"] = build_token_cost_advisor(
        db=db,
        tenant_id=job.tenant_id,
        provider_slug=run.provider_slug,
        model=run.model_used,
        api_key=None,
        api_base=None,
        api_version=None,
        system_prompt=system_prompt,
        input_payload=messages if messages else (rendered_input or ""),
        params=params,
        metadata=metadata,
    )
    if dropped_params:
        usage_payload["dropped_unsupported_params"] = dropped_params
    if service_tier:
        usage_payload["service_tier"] = service_tier
    job.usage_json = usage_payload

    body: dict[str, Any] = {
        "model": run.model_used,
        "instructions": system_prompt,
        "input": messages if messages else (rendered_input or ""),
        **request_params,
    }

    return body, None


def _apply_batch_output_line(
    db: Session,
    *,
    run: ProviderBatchRun,
    version: EndpointVersion,
    job: Job,
    line_payload: dict[str, Any],
) -> None:
    line_error = line_payload.get("error")
    if line_error:
        message = line_error if isinstance(line_error, str) else json.dumps(line_error)
        _mark_job_failed(db, job, f"Provider batch item error: {message}")
        return

    response = line_payload.get("response")
    if not isinstance(response, dict):
        _mark_job_failed(db, job, "Provider batch item has no response payload")
        return

    status_code = int(response.get("status_code") or 0)
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    if status_code < 200 or status_code >= 300:
        _mark_job_failed(
            db,
            job,
            f"Provider batch item failed (HTTP {status_code}): {body or response}",
        )
        return

    output_text = extract_response_text_from_dict(body)
    blocked_output_phrases = parse_list_param(version.params_json or {}, "blocked_output_phrases")
    blocked_output = find_blocked_phrase(output_text or "", blocked_output_phrases)
    if blocked_output:
        _mark_job_failed(db, job, f"Output blocked by phrase: {blocked_output}")
        return

    usage_payload = dict(job.usage_json or {})
    usage = extract_usage_from_dict(body)
    if usage:
        usage_payload.update(usage)
    usage_payload.update(
        {
            "batch_mode": "provider_native",
            "selected_provider": run.provider_slug,
            "selected_model": run.model_used,
            "attempted_routes": [{"provider": run.provider_slug, "model": run.model_used}],
            "cache_hit": False,
        }
    )

    estimated_cost_usd, pricing_details = estimate_job_cost_usd(
        db,
        tenant_id=job.tenant_id,
        provider_slug=run.provider_slug,
        model=run.model_used,
        usage_json=usage_payload,
    )
    if pricing_details is not None:
        usage_payload["pricing"] = pricing_details

    job.result_text = output_text
    job.provider_response_id = body.get("id") if isinstance(body.get("id"), str) else response.get("request_id")
    job.usage_json = usage_payload
    job.estimated_cost_usd = estimated_cost_usd
    job.provider_used = run.provider_slug
    job.model_used = run.model_used
    job.error = None
    job.cache_hit = False
    job.cached_from_job_id = None
    job.provider_batch_status = "completed"

    if job.cancel_requested:
        job.status = JobStatus.canceled
        job.provider_batch_status = "canceled"
    else:
        job.status = JobStatus.completed
    job.finished_at = datetime.now(UTC)
    db.add(job)

    if job.status == JobStatus.completed:
        auto_save_training_event(db, job)


def _schedule_next_poll(run_id: str, attempt: int) -> None:
    settings = get_settings()
    queue = get_queue()
    delay = timedelta(seconds=max(1, int(settings.provider_batch_poll_interval_seconds)))
    queue.enqueue_in(
        delay,
        "app.tasks.poll_provider_batch_run",
        run_id,
        attempt,
        job_timeout=settings.job_timeout_seconds,
    )


def _iter_output_file_lines(client: Any, file_id: str):
    streaming_api = getattr(client.files, "with_streaming_response", None)
    if streaming_api is not None and hasattr(streaming_api, "content"):
        stream_obj = streaming_api.content(file_id)
        close_fn = getattr(stream_obj, "close", None)
        try:
            iter_lines = getattr(stream_obj, "iter_lines", None)
            if callable(iter_lines):
                for raw_line in iter_lines():
                    if isinstance(raw_line, bytes):
                        yield raw_line.decode("utf-8", errors="replace")
                    else:
                        yield str(raw_line)
                return
        finally:
            if callable(close_fn):
                close_fn()

    # Fallback for older SDK behavior.
    raw_content = client.files.retrieve_content(file_id)
    if isinstance(raw_content, bytes):
        for raw_line in raw_content.splitlines():
            yield raw_line.decode("utf-8", errors="replace")
        return

    for raw_line in str(raw_content).splitlines():
        yield raw_line


def submit_provider_batch_run_task(run_id: str) -> None:
    db = SessionLocal()
    try:
        run = db.get(ProviderBatchRun, run_id)
        if run is None or run.status in TERMINAL_BATCH_STATUSES:
            return

        if run.cancel_requested and run.status == "queued":
            run.status = "canceled"
            run.finished_at = datetime.now(UTC)
            jobs = list_jobs_for_provider_batch(db, run.tenant_id, run.id)
            for job in jobs:
                if job.status == JobStatus.queued:
                    _mark_job_canceled(db, job)
            refresh_provider_batch_counts(db, run)
            db.commit()
            return

        version = db.scalar(
            select(EndpointVersion).where(
                EndpointVersion.id == run.endpoint_version_id,
                EndpointVersion.endpoint_id == run.endpoint_id,
            )
        )
        if version is None:
            run.status = "failed"
            run.error = "Endpoint version not found"
            run.finished_at = datetime.now(UTC)
            jobs = list_jobs_for_provider_batch(db, run.tenant_id, run.id)
            for job in jobs:
                if job.status == JobStatus.queued:
                    _mark_job_failed(db, job, "Endpoint version not found")
            refresh_provider_batch_counts(db, run)
            db.commit()
            return

        credentials = resolve_provider_credentials(
            db,
            tenant_id=run.tenant_id,
            provider_slug=run.provider_slug,
            provider_config_id=run.provider_config_id,
        )
        client = build_provider_client(
            provider_slug=credentials.provider_slug,
            api_key=credentials.api_key or "",
            api_base=credentials.api_base,
            api_version=credentials.api_version,
            timeout_seconds=120,
            max_retries=2,
        )

        run.status = "submitting"
        run.started_at = run.started_at or datetime.now(UTC)
        run.error = None
        db.add(run)
        db.commit()

        jobs = list_jobs_for_provider_batch(db, run.tenant_id, run.id)
        persona = None
        if version.persona_id:
            persona = db.scalar(
                select(Persona).where(Persona.id == version.persona_id, Persona.tenant_id == run.tenant_id)
            )
        context_blocks = list_context_blocks_for_version(db, run.tenant_id, version.id)

        lines: list[str] = []
        for job in jobs:
            if job.status != JobStatus.queued:
                continue
            body, error_message = _prepare_batch_request_body(
                db,
                run=run,
                version=version,
                job=job,
                persona=persona,
                context_blocks=context_blocks,
            )
            if error_message:
                _mark_job_failed(db, job, error_message)
                continue

            payload = {
                "custom_id": job.id,
                "method": "POST",
                "url": "/v1/responses",
                "body": body,
            }
            lines.append(json.dumps(payload))
            job.provider_batch_status = "submitted"
            db.add(job)

        if not lines:
            run.status = "failed"
            run.error = "No valid jobs to submit after validation"
            run.finished_at = datetime.now(UTC)
            refresh_provider_batch_counts(db, run)
            db.add(run)
            db.commit()
            return

        batch_input = ("\n".join(lines) + "\n").encode("utf-8")
        file_stream = io.BytesIO(batch_input)
        file_stream.name = f"{run.id}.jsonl"

        uploaded = client.files.create(file=file_stream, purpose="batch")
        created_batch = client.batches.create(
            input_file_id=uploaded.id,
            endpoint="/v1/responses",
            completion_window=run.completion_window,
            metadata={
                "provider_batch_run_id": run.id,
                "endpoint_id": run.endpoint_id,
            },
        )

        batch_payload = _to_dict(created_batch)
        run.provider_batch_id = created_batch.id
        run.input_file_id = uploaded.id
        run.output_file_id = batch_payload.get("output_file_id")
        run.error_file_id = batch_payload.get("error_file_id")
        run.result_json = batch_payload
        run.status = _batch_status_from_provider(batch_payload.get("status"))
        run.last_polled_at = datetime.now(UTC)
        run.next_poll_at = datetime.now(UTC) + timedelta(seconds=get_settings().provider_batch_poll_interval_seconds)
        refresh_provider_batch_counts(db, run)
        db.add(run)
        db.commit()

        _schedule_next_poll(run.id, 0)
    except Exception as exc:  # noqa: BLE001
        run = db.get(ProviderBatchRun, run_id)
        if run is not None and run.status not in TERMINAL_BATCH_STATUSES:
            run.status = "failed"
            run.error = str(exc)
            run.finished_at = datetime.now(UTC)
            jobs = list_jobs_for_provider_batch(db, run.tenant_id, run.id)
            for job in jobs:
                if job.status == JobStatus.queued:
                    _mark_job_failed(db, job, f"Batch submission failed: {exc}")
            refresh_provider_batch_counts(db, run)
            db.add(run)
            db.commit()
    finally:
        db.close()


def poll_provider_batch_run_task(run_id: str, attempt: int = 0) -> None:
    db = SessionLocal()
    try:
        run = db.get(ProviderBatchRun, run_id)
        if run is None or run.status in TERMINAL_BATCH_STATUSES:
            return

        if not run.provider_batch_id:
            if run.status == "queued":
                _schedule_next_poll(run.id, attempt + 1)
                return
            run.status = "failed"
            run.error = "Provider batch id missing"
            run.finished_at = datetime.now(UTC)
            refresh_provider_batch_counts(db, run)
            db.add(run)
            db.commit()
            return

        settings = get_settings()
        if attempt >= max(1, int(settings.provider_batch_max_poll_attempts)):
            run.status = "failed"
            run.error = "Polling timeout reached"
            run.finished_at = datetime.now(UTC)
            jobs = list_jobs_for_provider_batch(db, run.tenant_id, run.id)
            for job in jobs:
                if job.status == JobStatus.queued:
                    _mark_job_failed(db, job, "Polling timeout reached")
            refresh_provider_batch_counts(db, run)
            db.add(run)
            db.commit()
            return

        credentials = resolve_provider_credentials(
            db,
            tenant_id=run.tenant_id,
            provider_slug=run.provider_slug,
            provider_config_id=run.provider_config_id,
        )
        client = build_provider_client(
            provider_slug=credentials.provider_slug,
            api_key=credentials.api_key or "",
            api_base=credentials.api_base,
            api_version=credentials.api_version,
            timeout_seconds=120,
            max_retries=2,
        )

        if run.cancel_requested and run.provider_batch_id:
            try:
                client.batches.cancel(run.provider_batch_id)
            except Exception:
                # Best effort. Continue polling state.
                pass

        provider_batch = client.batches.retrieve(run.provider_batch_id)
        batch_payload = _to_dict(provider_batch)

        run.status = _batch_status_from_provider(batch_payload.get("status"))
        run.output_file_id = batch_payload.get("output_file_id") or run.output_file_id
        run.error_file_id = batch_payload.get("error_file_id") or run.error_file_id
        run.result_json = batch_payload
        run.last_polled_at = datetime.now(UTC)

        version = db.scalar(
            select(EndpointVersion).where(
                EndpointVersion.id == run.endpoint_version_id,
                EndpointVersion.endpoint_id == run.endpoint_id,
            )
        )
        if version is None:
            run.status = "failed"
            run.error = "Endpoint version not found during poll"
            run.finished_at = datetime.now(UTC)
            db.add(run)
            db.commit()
            return

        jobs = {job.id: job for job in list_jobs_for_provider_batch(db, run.tenant_id, run.id)}

        if run.output_file_id:
            try:
                for line in _iter_output_file_lines(client, run.output_file_id):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        line_payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    custom_id = line_payload.get("custom_id")
                    if not isinstance(custom_id, str):
                        continue
                    job = jobs.get(custom_id)
                    if job is None or job.status in {JobStatus.completed, JobStatus.failed, JobStatus.canceled}:
                        continue
                    _apply_batch_output_line(
                        db,
                        run=run,
                        version=version,
                        job=job,
                        line_payload=line_payload,
                    )
            except Exception as exc:  # noqa: BLE001
                run.error = f"Failed to parse batch output: {exc}"

        terminal_provider_status = run.status in TERMINAL_BATCH_STATUSES
        if terminal_provider_status:
            remaining_jobs = list_jobs_for_provider_batch(db, run.tenant_id, run.id)
            for job in remaining_jobs:
                if job.status == JobStatus.queued:
                    if run.status == "canceled" or run.cancel_requested:
                        _mark_job_canceled(db, job)
                    else:
                        _mark_job_failed(db, job, "No result returned for this batch item")
            run.finished_at = datetime.now(UTC)

        refresh_provider_batch_counts(db, run)
        if run.status in TERMINAL_BATCH_STATUSES:
            run.next_poll_at = None
        else:
            run.next_poll_at = datetime.now(UTC) + timedelta(seconds=settings.provider_batch_poll_interval_seconds)

        db.add(run)
        db.commit()

        if run.status not in TERMINAL_BATCH_STATUSES:
            _schedule_next_poll(run.id, attempt + 1)
    except Exception as exc:  # noqa: BLE001
        run = db.get(ProviderBatchRun, run_id)
        if run is not None and run.status not in TERMINAL_BATCH_STATUSES:
            run.status = "failed"
            run.error = str(exc)
            run.finished_at = datetime.now(UTC)
            jobs = list_jobs_for_provider_batch(db, run.tenant_id, run.id)
            for job in jobs:
                if job.status == JobStatus.queued:
                    _mark_job_failed(db, job, f"Batch poll failed: {exc}")
            refresh_provider_batch_counts(db, run)
            db.add(run)
            db.commit()
    finally:
        db.close()
