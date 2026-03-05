import random
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.provider_catalog import ensure_supported_provider_slug, normalize_provider_slug
from app.core.redis_client import get_redis
from app.models import EndpointVersion, Job, JobStatus, Persona, Target
from app.services.llm import run_provider_completion
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
from app.services.provider_batches import (
    poll_provider_batch_run_task,
    submit_provider_batch_run_task,
)
from app.services.providers import resolve_provider_credentials
from app.services.token_advisor import build_token_cost_advisor
from app.services.training import auto_save_training_event, list_few_shot_examples


def _cache_key(job: Job) -> str:
    return f"response_cache:{job.tenant_id}:{job.endpoint_id}:{job.endpoint_version_id}:{job.request_hash}"


def _mark_job_failed(db, job: Job, message: str) -> None:
    job.status = JobStatus.failed
    job.error = message
    job.estimated_cost_usd = None
    job.finished_at = datetime.now(UTC)
    db.add(job)
    db.commit()


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


def _parse_fallback_targets(
    primary_provider: str,
    params: dict[str, Any],
) -> list[tuple[str, str]]:
    if not _parse_bool_param(params, "enable_fallbacks", default=False):
        return []

    routes: list[tuple[str, str]] = []

    fallback_targets = params.get("fallback_targets")
    if isinstance(fallback_targets, list):
        for item in fallback_targets:
            if isinstance(item, dict):
                provider = item.get("provider")
                model = item.get("model")
                if isinstance(model, str) and model.strip():
                    resolved_provider = primary_provider
                    if isinstance(provider, str):
                        try:
                            resolved_provider = ensure_supported_provider_slug(provider)
                        except ValueError:
                            continue
                    routes.append((resolved_provider, model.strip()))
                continue

            if isinstance(item, str):
                value = item.strip()
                if not value:
                    continue
                if "/" in value:
                    prefix, model = value.split("/", 1)
                    try:
                        resolved_prefix = ensure_supported_provider_slug(prefix)
                    except ValueError:
                        continue
                    routes.append((resolved_prefix, model.strip()))
                else:
                    routes.append((primary_provider, value))

    # Backward compatibility with legacy fallback_models list.
    for model in parse_list_param(params, "fallback_models"):
        routes.append((primary_provider, model))

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for provider, model in routes:
        key = (provider, model)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _candidate_routes(version: EndpointVersion, params: dict[str, Any]) -> list[tuple[str, str]]:
    try:
        primary_provider = ensure_supported_provider_slug(version.provider or "openai")
    except ValueError:
        primary_provider = normalize_provider_slug("openai")
    routes: list[tuple[str, str]] = [(primary_provider, version.model)]
    routes.extend(_parse_fallback_targets(primary_provider, params))

    strategy = str(params.get("routing_strategy", "ordered")).lower().strip()
    if strategy == "random" and len(routes) > 1:
        primary = routes[0]
        remainder = routes[1:]
        random.shuffle(remainder)
        routes = [primary, *remainder]

    max_attempts = parse_int_param(params, "max_route_attempts", default=len(routes), min_value=1, max_value=25)
    return routes[:max_attempts]


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


def process_job(job_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return

        if job.status == JobStatus.canceled:
            return

        job.status = JobStatus.running
        job.started_at = datetime.now(UTC)
        db.add(job)
        db.commit()
        db.refresh(job)

        version = db.scalar(select(EndpointVersion).where(EndpointVersion.id == job.endpoint_version_id))
        if version is None:
            _mark_job_failed(db, job, "Endpoint version not found")
            return
        selected_target = None
        if version.target_id:
            selected_target = db.scalar(
                select(Target).where(Target.id == version.target_id, Target.tenant_id == job.tenant_id)
            )

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
        primary_provider = normalize_provider_slug(version.provider or "openai")
        manual_provider_config_id: str | None = None
        raw_provider_config_id = params.get("provider_config_id")
        if isinstance(raw_provider_config_id, str) and raw_provider_config_id.strip():
            manual_provider_config_id = raw_provider_config_id.strip()
        request_hash = build_request_hash(
            endpoint_version_id=version.id,
            input_text=rendered_input,
            messages=messages,
            metadata=metadata,
        )
        job.request_hash = request_hash
        if rendered_input is not None:
            job.request_json = {**req, "rendered_input": rendered_input}
        db.add(job)
        db.commit()

        cache_ttl_seconds = parse_int_param(params, "cache_ttl_seconds", default=0, min_value=0, max_value=604800)
        redis = get_redis()
        if cache_ttl_seconds > 0 and job.request_hash:
            source_job_id = redis.get(_cache_key(job))
            if source_job_id:
                source_job = db.get(Job, source_job_id)
                if source_job and source_job.status == JobStatus.completed and source_job.result_text is not None:
                    cached_usage = {**(source_job.usage_json or {}), "cache_hit": True, "cache_reused_from_job_id": source_job.id}
                    if isinstance(cached_usage.get("pricing"), dict):
                        cached_usage["pricing"] = {
                            **cached_usage["pricing"],
                            "estimated_cost_usd": 0.0,
                            "cache_hit": True,
                        }
                    job.result_text = source_job.result_text
                    job.provider_response_id = source_job.provider_response_id
                    job.usage_json = cached_usage
                    job.provider_used = source_job.provider_used
                    job.model_used = source_job.model_used
                    job.estimated_cost_usd = 0.0
                    job.error = None
                    job.cache_hit = True
                    job.cached_from_job_id = source_job.id
                    job.status = JobStatus.canceled if job.cancel_requested else JobStatus.completed
                    job.finished_at = datetime.now(UTC)
                    db.add(job)
                    db.commit()
                    db.refresh(job)
                    if job.status == JobStatus.completed:
                        auto_save_training_event(db, job)
                    return

        persona = None
        if version.persona_id:
            persona = db.scalar(select(Persona).where(Persona.id == version.persona_id, Persona.tenant_id == job.tenant_id))
        context_blocks = list_context_blocks_for_version(db, job.tenant_id, version.id)
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

        blocked_input_phrases = parse_list_param(params, "blocked_input_phrases")
        request_text = collect_request_text(rendered_input, messages)
        blocked_input = find_blocked_phrase(request_text, blocked_input_phrases)
        if blocked_input:
            _mark_job_failed(db, job, f"Input blocked by phrase: {blocked_input}")
            return

        try:
            blocked_output_phrases = parse_list_param(params, "blocked_output_phrases")
            timeout_seconds = parse_int_param(params, "timeout_seconds", default=60, min_value=1, max_value=600)
            max_retries = parse_int_param(params, "max_retries", default=1, min_value=0, max_value=10)
            candidate_routes = _candidate_routes(version, params)

            text: str | None = None
            provider_id: str | None = None
            usage: dict | None = None
            selected_model: str | None = None
            selected_provider: str | None = None
            selected_advisor: dict[str, Any] | None = None
            route_errors: list[str] = []
            attempted_routes: list[dict[str, str]] = []

            for candidate_provider, candidate_model in candidate_routes:
                attempted_routes.append({"provider": candidate_provider, "model": candidate_model})
                try:
                    provider_config_id = None
                    if (
                        selected_target is not None
                        and selected_target.provider_slug == candidate_provider
                        and selected_target.model_identifier == candidate_model
                    ):
                        provider_config_id = selected_target.provider_config_id
                    elif manual_provider_config_id is not None and candidate_provider == primary_provider:
                        provider_config_id = manual_provider_config_id

                    credentials = resolve_provider_credentials(
                        db,
                        tenant_id=job.tenant_id,
                        provider_slug=candidate_provider,
                        provider_config_id=provider_config_id,
                    )
                    candidate_advisor = build_token_cost_advisor(
                        db=db,
                        tenant_id=job.tenant_id,
                        provider_slug=credentials.provider_slug,
                        model=candidate_model,
                        api_key=credentials.api_key,
                        api_base=credentials.api_base,
                        api_version=credentials.api_version,
                        system_prompt=system_prompt,
                        input_payload=messages if messages else (rendered_input or ""),
                        params=params,
                        metadata=metadata,
                    )
                    text, provider_id, usage = run_provider_completion(
                        provider_slug=credentials.provider_slug,
                        model=candidate_model,
                        api_key=credentials.api_key,
                        api_base=credentials.api_base,
                        api_version=credentials.api_version,
                        system_prompt=system_prompt,
                        input_payload=messages if messages else (rendered_input or ""),
                        params=params,
                        timeout_seconds=timeout_seconds,
                        max_retries=max_retries,
                    )
                except Exception as exc:  # noqa: BLE001
                    route_errors.append(f"{candidate_provider}/{candidate_model}: {exc}")
                    continue

                blocked_output = find_blocked_phrase(text or "", blocked_output_phrases)
                if blocked_output:
                    route_errors.append(f"{candidate_provider}/{candidate_model}: output blocked by phrase {blocked_output}")
                    continue

                selected_provider = candidate_provider
                selected_model = candidate_model
                selected_advisor = candidate_advisor
                break

            if selected_model is None or selected_provider is None:
                _mark_job_failed(
                    db,
                    job,
                    "All model attempts failed. " + (" | ".join(route_errors) if route_errors else "No successful provider call."),
                )
                return

            usage_payload = usage or {}
            usage_payload.update(
                {
                    "attempted_routes": attempted_routes,
                    "selected_provider": selected_provider,
                    "selected_model": selected_model,
                    "cache_hit": False,
                    "persona": persona.name if persona else None,
                    "context_blocks": [block.name for block in context_blocks],
                    "few_shot_enabled": few_shot_enabled,
                    "few_shot_count": few_shot_count,
                }
            )
            if selected_advisor is not None:
                usage_payload["advisor"] = selected_advisor
            estimated_cost_usd, pricing_details = estimate_job_cost_usd(
                db,
                tenant_id=job.tenant_id,
                provider_slug=selected_provider,
                model=selected_model,
                usage_json=usage_payload,
            )
            if pricing_details is not None:
                usage_payload["pricing"] = pricing_details

            job.result_text = text
            job.provider_response_id = provider_id
            job.usage_json = usage_payload
            job.estimated_cost_usd = estimated_cost_usd
            job.provider_used = selected_provider
            job.model_used = selected_model
            job.error = None
            job.cache_hit = False
            job.cached_from_job_id = None

            if job.cancel_requested:
                job.status = JobStatus.canceled
            else:
                job.status = JobStatus.completed
            job.finished_at = datetime.now(UTC)
            db.add(job)
            db.commit()
            db.refresh(job)

            if job.status == JobStatus.completed:
                if cache_ttl_seconds > 0 and job.request_hash:
                    redis.setex(_cache_key(job), cache_ttl_seconds, job.id)
                auto_save_training_event(db, job)
        except Exception as exc:  # noqa: BLE001
            _mark_job_failed(db, job, str(exc))
    finally:
        db.close()


def submit_provider_batch_run(run_id: str) -> None:
    submit_provider_batch_run_task(run_id)


def poll_provider_batch_run(run_id: str, attempt: int = 0) -> None:
    poll_provider_batch_run_task(run_id, attempt=attempt)
