"""Model-parameter validation against provider/model capability registry.

This service normalizes request params from UI/API into a safe, model-aware
shape before runtime execution. It prevents unsupported parameter usage and
enforces bounds/type constraints declared in provider YAML specs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.provider_registry import ModelParameterSpec, get_model_spec, list_models_for_provider


class ModelParamValidationError(ValueError):
    """Raised when model parameters violate provider capability declarations."""

    pass


@dataclass(frozen=True)
class ModelParamValidationResult:
    """Normalized params + non-fatal warnings from validation phase."""

    params: dict[str, Any]
    warnings: list[str]


RUNTIME_PARAM_KEYS = {
    "timeout_seconds",
    "max_retries",
    "cache_ttl_seconds",
    "blocked_input_phrases",
    "blocked_output_phrases",
    "few_shot_enabled",
    "few_shot_limit",
    "enable_fallbacks",
    "routing_strategy",
    "fallback_targets",
    "fallback_models",
    "max_route_attempts",
    "metadata",
    "provider_config_id",
}


def _parse_bool(value: Any) -> bool:
    """Parse broad boolean-like inputs used by form and API payloads."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ValueError("must be a boolean")


def _extract_param_value(params: dict[str, Any], key: str) -> tuple[bool, Any]:
    """Fetch model parameter value, including normalized alias lookups."""
    if key == "reasoning_effort":
        if "reasoning_effort" in params:
            return True, params.get("reasoning_effort")
        reasoning = params.get("reasoning")
        if isinstance(reasoning, dict) and "effort" in reasoning:
            return True, reasoning.get("effort")
        return False, None
    if key in params:
        return True, params.get(key)
    return False, None


def _assign_param_value(params: dict[str, Any], key: str, value: Any) -> None:
    """Write normalized parameter back to output dictionary."""
    if key == "reasoning_effort":
        existing = params.get("reasoning")
        reasoning_payload = dict(existing) if isinstance(existing, dict) else {}
        reasoning_payload["effort"] = str(value)
        params["reasoning"] = reasoning_payload
        params.pop("reasoning_effort", None)
        return
    params[key] = value


def _coerce_and_validate_value(spec: ModelParameterSpec, value: Any) -> Any:
    """Coerce one raw value to declared type and enforce constraints."""
    param_type = (spec.param_type or "").strip().lower()
    if not param_type:
        return value

    if param_type == "integer":
        if isinstance(value, bool):
            raise ValueError("must be an integer")
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, float) and value.is_integer():
            parsed = int(value)
        elif isinstance(value, str):
            parsed = int(value.strip())
        else:
            raise ValueError("must be an integer")
    elif param_type == "number":
        if isinstance(value, bool):
            raise ValueError("must be a number")
        if isinstance(value, (int, float)):
            parsed = float(value)
        elif isinstance(value, str):
            parsed = float(value.strip())
        else:
            raise ValueError("must be a number")
    elif param_type == "boolean":
        parsed = _parse_bool(value)
    elif param_type == "enum":
        parsed = str(value).strip()
        allowed = list(spec.values)
        if allowed and parsed not in allowed:
            raise ValueError(f"must be one of {', '.join(allowed)}")
    else:
        parsed = str(value).strip()
        if parsed == "":
            raise ValueError("must be a non-empty string")

    if isinstance(parsed, (int, float)):
        if spec.min_value is not None and parsed < spec.min_value:
            raise ValueError(f"must be >= {spec.min_value}")
        if spec.max_value is not None and parsed > spec.max_value:
            raise ValueError(f"must be <= {spec.max_value}")
    return parsed


def _known_provider_param_keys(provider_slug: str) -> set[str]:
    """Collect all known parameter keys across provider model declarations."""
    keys: set[str] = set()
    for model_spec in list_models_for_provider(provider_slug):
        for item in model_spec.parameters:
            keys.add(item.key)
    keys.add("reasoning")
    return keys


def validate_model_params(
    *,
    provider_slug: str,
    model: str,
    params: dict[str, Any] | None,
    strict_known_keys: bool = True,
) -> ModelParamValidationResult:
    """Validate and normalize model parameters for one provider/model route.

    Unknown runtime control keys listed in ``RUNTIME_PARAM_KEYS`` are preserved.
    Model-specific keys are validated when model exists in registry, while
    custom model identifiers skip strict checks to support custom deployments.
    """
    cleaned_params = dict(params or {})
    model_id = (model or "").strip()
    if not model_id:
        raise ModelParamValidationError("Model is required.")

    model_spec = get_model_spec(provider_slug, model_id)
    if model_spec is None:
        # Custom deployment/model identifiers are allowed when not in registry.
        return ModelParamValidationResult(params=cleaned_params, warnings=["Model is not in catalog; skipped strict parameter validation."])

    spec_map = {item.key: item for item in model_spec.parameters}
    warnings: list[str] = []
    errors: list[str] = []

    for key, spec in spec_map.items():
        present, raw_value = _extract_param_value(cleaned_params, key)
        if not present:
            continue
        if raw_value is None or (isinstance(raw_value, str) and raw_value.strip() == ""):
            continue

        if not spec.supported:
            errors.append(f"{key} is not supported for {provider_slug}/{model_id}.")
            continue

        try:
            coerced = _coerce_and_validate_value(spec, raw_value)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key} {exc}.")
            continue
        _assign_param_value(cleaned_params, key, coerced)

    if strict_known_keys:
        known_provider_keys = _known_provider_param_keys(provider_slug)
        for key in list(cleaned_params.keys()):
            if key in RUNTIME_PARAM_KEYS:
                continue
            if key == "reasoning":
                continue
            if key not in known_provider_keys:
                continue
            if key in spec_map:
                continue
            errors.append(f"{key} is not valid for {provider_slug}/{model_id}.")

    if errors:
        raise ModelParamValidationError(" ".join(errors))

    return ModelParamValidationResult(params=cleaned_params, warnings=warnings)
