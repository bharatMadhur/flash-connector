from __future__ import annotations

from statistics import median
from typing import Any
from urllib.parse import urljoin

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.provider_profiles import is_azure_provider_slug
from app.models import Job, JobStatus
from app.services.pricing import estimate_job_cost_usd


def _text_from_input_payload(input_payload: str | list[dict[str, Any]]) -> str:
    if isinstance(input_payload, str):
        return input_payload

    chunks: list[str] = []
    for message in input_payload:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "\n".join(chunks)


def _heuristic_token_estimate(system_prompt: str, input_payload: str | list[dict[str, Any]]) -> int:
    rendered_input = _text_from_input_payload(input_payload)
    combined = f"{system_prompt}\n{rendered_input}".strip()
    if not combined:
        return 1
    return max(1, len(combined) // 4)


def _safe_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _extract_preflight_tokens(payload: Any) -> int | None:
    if isinstance(payload, dict):
        direct_keys = (
            "input_tokens",
            "prompt_tokens",
            "total_input_tokens",
            "total_prompt_tokens",
            "tokens",
        )
        for key in direct_keys:
            parsed = _safe_int(payload.get(key))
            if parsed is not None:
                return parsed

        usage = payload.get("usage")
        if isinstance(usage, dict):
            parsed = _extract_preflight_tokens(usage)
            if parsed is not None:
                return parsed

        for nested_value in payload.values():
            if isinstance(nested_value, dict):
                parsed = _extract_preflight_tokens(nested_value)
                if parsed is not None:
                    return parsed
    return None


def _extract_input_tokens_from_usage(usage_json: dict[str, Any] | None) -> int | None:
    if not isinstance(usage_json, dict):
        return None
    for key in ("input_tokens", "prompt_tokens", "total_input_tokens"):
        parsed = _safe_int(usage_json.get(key))
        if parsed is not None:
            return parsed
    return None


def _normalize_openai_base(api_base: str | None) -> str:
    default = "https://api.openai.com/v1/"
    if not api_base:
        return default
    cleaned = api_base.strip().rstrip("/")
    if not cleaned:
        return default
    if cleaned.endswith("/v1"):
        return f"{cleaned}/"
    if cleaned.endswith("/openai/v1"):
        return f"{cleaned}/"
    if cleaned.endswith("/openai"):
        return f"{cleaned}/v1/"
    if "/v1" in cleaned:
        return f"{cleaned}/"
    return f"{cleaned}/v1/"


def _openai_preflight_input_tokens(
    *,
    api_key: str,
    api_base: str | None,
    model: str,
    system_prompt: str,
    input_payload: str | list[dict[str, Any]],
) -> tuple[int | None, str]:
    base = _normalize_openai_base(api_base)
    endpoint = urljoin(base, "responses/input_tokens")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "instructions": system_prompt or "",
        "input": input_payload,
    }
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.post(endpoint, headers=headers, json=payload)
        if response.status_code >= 400:
            return None, f"provider_http_{response.status_code}"
        parsed = _extract_preflight_tokens(response.json())
        if parsed is None:
            return None, "provider_unparsable"
        return parsed, "openai_preflight"
    except Exception:  # noqa: BLE001
        return None, "provider_unavailable"


def _normalize_azure_base(api_base: str | None) -> str | None:
    if not api_base:
        return None
    cleaned = api_base.strip().rstrip("/")
    if not cleaned:
        return None
    return cleaned


def _azure_preflight_input_tokens(
    *,
    api_key: str,
    api_base: str | None,
    api_version: str | None,
    model: str,
    system_prompt: str,
    input_payload: str | list[dict[str, Any]],
) -> tuple[int | None, str]:
    normalized_base = _normalize_azure_base(api_base)
    if not normalized_base:
        return None, "provider_unavailable"

    if "/openai/v1" in normalized_base:
        candidate_endpoints = [(f"{normalized_base}/responses/input_tokens", None)]
    elif normalized_base.endswith("/models"):
        candidate_endpoints = [(f"{normalized_base}/responses/input_tokens", None)]
    else:
        version_params = {"api-version": api_version or "2024-10-21"}
        candidate_endpoints = [
            (f"{normalized_base}/openai/v1/responses/input_tokens", None),
            (f"{normalized_base}/openai/responses/input_tokens", version_params),
        ]

    headers = {"api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "model": model,
        "instructions": system_prompt or "",
        "input": input_payload,
    }

    for endpoint, params in candidate_endpoints:
        try:
            with httpx.Client(timeout=8.0) as client:
                response = client.post(endpoint, headers=headers, json=payload, params=params)
            if response.status_code == 404:
                continue
            if response.status_code >= 400:
                return None, f"provider_http_{response.status_code}"
            parsed = _extract_preflight_tokens(response.json())
            if parsed is None:
                return None, "provider_unparsable"
            return parsed, "azure_preflight"
        except Exception:  # noqa: BLE001
            continue
    return None, "provider_unavailable"


def _request_text_from_job(job: Job) -> str:
    request_json = job.request_json if isinstance(job.request_json, dict) else {}
    rendered = request_json.get("rendered_input")
    if isinstance(rendered, str) and rendered.strip():
        return rendered
    input_text = request_json.get("input")
    if isinstance(input_text, str):
        return input_text
    messages = request_json.get("messages")
    if isinstance(messages, list):
        return _text_from_input_payload(messages)
    return ""


def _historical_input_estimate(
    *,
    db: Session,
    tenant_id: str,
    provider_slug: str,
    model: str,
    system_prompt: str,
    input_payload: str | list[dict[str, Any]],
) -> int | None:
    candidate_jobs = db.scalars(
        select(Job)
        .where(
            Job.tenant_id == tenant_id,
            Job.status == JobStatus.completed,
            Job.provider_used == provider_slug,
            Job.model_used == model,
            Job.usage_json.is_not(None),
        )
        .order_by(Job.finished_at.desc(), Job.created_at.desc())
        .limit(120)
    ).all()

    ratios: list[float] = []
    for job in candidate_jobs:
        usage_tokens = _extract_input_tokens_from_usage(job.usage_json if isinstance(job.usage_json, dict) else None)
        if usage_tokens is None or usage_tokens <= 0:
            continue
        request_text = _request_text_from_job(job)
        if not request_text:
            continue
        ratios.append(float(usage_tokens) / max(len(request_text), 1))

    if not ratios:
        return None

    inferred_ratio = median(ratios)
    current_text = f"{system_prompt}\n{_text_from_input_payload(input_payload)}".strip()
    if not current_text:
        return 1
    return max(1, int(len(current_text) * inferred_ratio))


def _cacheability_score(
    *,
    params: dict[str, Any],
    metadata: dict[str, Any],
    input_payload: str | list[dict[str, Any]],
) -> tuple[int, list[str]]:
    score = 60
    tips: list[str] = []

    cache_ttl = 0
    try:
        cache_ttl = int(params.get("cache_ttl_seconds", 0) or 0)
    except (TypeError, ValueError):
        cache_ttl = 0
    if cache_ttl > 0:
        score += 18
    else:
        score -= 18
        tips.append("Enable response cache with a TTL for repeated prompts.")

    temperature = params.get("temperature")
    try:
        temp_value = float(temperature) if temperature is not None else None
    except (TypeError, ValueError):
        temp_value = None
    if temp_value is not None:
        if temp_value <= 0.2:
            score += 10
        elif temp_value >= 0.8:
            score -= 14
            tips.append("Lower temperature to improve deterministic cache reuse.")

    volatile_markers = {"timestamp", "time", "nonce", "uuid", "request_id", "trace_id", "session_id", "random"}
    metadata_keys = {str(key).strip().lower() for key in metadata.keys()}
    if metadata_keys.intersection(volatile_markers):
        score -= 20
        tips.append("Move volatile metadata fields out of the cache key path.")

    input_text = _text_from_input_payload(input_payload)
    if len(input_text) > 5000:
        score -= 10
        tips.append("Trim large context blocks and send only top relevant chunks.")
    if any(char.isdigit() for char in input_text) and len(input_text) > 200:
        score -= 5
        tips.append("Separate dynamic numeric values from static prompt prefixes.")

    clamped = max(0, min(score, 100))
    if clamped >= 75:
        tier = "high"
    elif clamped >= 45:
        tier = "medium"
    else:
        tier = "low"

    if tier == "low" and "Enable response cache with a TTL for repeated prompts." not in tips:
        tips.append("Increase reusable prompt prefix and keep request shape stable.")

    return clamped, tips[:3]


def build_token_cost_advisor(
    *,
    db: Session,
    tenant_id: str,
    provider_slug: str,
    model: str,
    api_key: str | None,
    api_base: str | None,
    api_version: str | None,  # kept for forward compatibility
    system_prompt: str,
    input_payload: str | list[dict[str, Any]],
    params: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    runtime_params = dict(params or {})
    request_metadata = dict(metadata or {})

    estimated_input_tokens = _heuristic_token_estimate(system_prompt, input_payload)
    estimate_source = "heuristic_chars_div4"

    if provider_slug == "openai" and api_key:
        preflight_tokens, source = _openai_preflight_input_tokens(
            api_key=api_key,
            api_base=api_base,
            model=model,
            system_prompt=system_prompt,
            input_payload=input_payload,
        )
        if preflight_tokens is not None:
            estimated_input_tokens = preflight_tokens
            estimate_source = source
        else:
            estimate_source = f"{source}_fallback"
    elif is_azure_provider_slug(provider_slug) and api_key:
        preflight_tokens, source = _azure_preflight_input_tokens(
            api_key=api_key,
            api_base=api_base,
            api_version=api_version,
            model=model,
            system_prompt=system_prompt,
            input_payload=input_payload,
        )
        if preflight_tokens is not None:
            estimated_input_tokens = preflight_tokens
            estimate_source = source
        else:
            estimate_source = f"{source}_fallback"

    if estimate_source.endswith("_fallback"):
        historical_estimate = _historical_input_estimate(
            db=db,
            tenant_id=tenant_id,
            provider_slug=provider_slug,
            model=model,
            system_prompt=system_prompt,
            input_payload=input_payload,
        )
        if historical_estimate is not None:
            estimated_input_tokens = historical_estimate
            estimate_source = "historical_ratio"

    if estimate_source in {"openai_preflight", "azure_preflight"}:
        estimate_confidence = "high"
    elif estimate_source == "historical_ratio":
        estimate_confidence = "medium"
    else:
        estimate_confidence = "low"

    try:
        output_target = max(1, int(runtime_params.get("max_output_tokens", 512) or 512))
    except (TypeError, ValueError):
        output_target = 512

    expected_cost_usd, pricing = estimate_job_cost_usd(
        db,
        tenant_id=tenant_id,
        provider_slug=provider_slug,
        model=model,
        usage_json={
            "input_tokens": estimated_input_tokens,
            "output_tokens": output_target,
            "cached_tokens": 0,
        },
    )

    cacheability_score, tips = _cacheability_score(
        params=runtime_params,
        metadata=request_metadata,
        input_payload=input_payload,
    )
    cacheability_band = "high" if cacheability_score >= 75 else "medium" if cacheability_score >= 45 else "low"

    return {
        "input_tokens_estimate": estimated_input_tokens,
        "estimate_source": estimate_source,
        "estimate_confidence": estimate_confidence,
        "output_tokens_target": output_target,
        "expected_cost_usd": expected_cost_usd,
        "pricing_preview": pricing,
        "cacheability_score": cacheability_score,
        "cacheability_band": cacheability_band,
        "tips": tips,
    }
