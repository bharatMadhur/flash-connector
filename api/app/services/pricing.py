"""Token usage and built-in cost estimation service.

This module intentionally keeps runtime billing logic deterministic for OSS:
- no tenant-level custom pricing overrides
- no manual UI pricing writes
- provider/model rates loaded from repository YAML declarations

The estimator outputs a best-effort `builtin_estimate` based on returned token usage.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from app.core.provider_catalog import ensure_supported_provider_slug


@dataclass(frozen=True)
class TokenUsage:
    """Normalized token usage tuple extracted from provider response payload."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int


@dataclass(frozen=True)
class BuiltinPricingRate:
    """One pricing rate declaration for model pattern matching."""

    model_pattern: str
    input_per_1m_usd: float
    output_per_1m_usd: float
    cached_input_per_1m_usd: float | None = None


@dataclass(frozen=True)
class ResolvedPricing:
    """Resolved pricing result used to compute estimated job cost."""

    source: str
    pricing_rate_id: str | None
    model_pattern: str
    input_per_1m_usd: float
    output_per_1m_usd: float
    cached_input_per_1m_usd: float | None


# Provider slugs -> pricing catalog key.
_PROVIDER_PRICING_CATALOG_MAP: dict[str, str] = {
    "openai": "openai",
    "azure_openai": "azure_openai",
    "azure_openai_v1": "azure_openai",
    "azure_openai_deployment": "azure_openai",
    "azure_ai_foundry": "azure_openai",
}


def _repo_root() -> Path:
    """Return repository root from current module path."""

    return Path(__file__).resolve().parents[3]


def _pricing_catalog_dir() -> Path:
    """Return directory containing built-in pricing YAML declarations."""

    return _repo_root() / "providers" / "pricing"


def _to_nonnegative_int(value: Any) -> int:
    """Convert raw numeric-like input into non-negative integer."""

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def normalize_model_pattern(value: str) -> str:
    """Normalize model wildcard pattern for deterministic matching."""

    cleaned = (value or "").strip().lower()
    return cleaned or "*"


def _is_wildcard_pattern(pattern: str) -> bool:
    """Return True when a model pattern contains wildcard tokens."""

    return any(ch in pattern for ch in "*?[]")


def _pattern_specificity(pattern: str) -> tuple[int, int]:
    """Score wildcard specificity for stable best-match selection."""

    normalized = normalize_model_pattern(pattern)
    literal_chars = len([ch for ch in normalized if ch not in "*?[]"])
    return (literal_chars, len(normalized))


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Load and validate one YAML mapping file from disk."""

    if not path.exists():
        raise RuntimeError(f"Missing pricing catalog file: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Pricing catalog must be a mapping: {path}")
    return raw


def _parse_rates_from_payload(path: Path, payload: dict[str, Any]) -> tuple[BuiltinPricingRate, ...]:
    """Parse one provider pricing payload into typed rate objects."""

    rates_raw = payload.get("rates")
    if not isinstance(rates_raw, list):
        raise RuntimeError(f"Pricing catalog missing list 'rates': {path}")

    rates: list[BuiltinPricingRate] = []
    for item in rates_raw:
        if not isinstance(item, dict):
            continue
        pattern = normalize_model_pattern(str(item.get("model_pattern", "")))
        if not pattern:
            continue
        try:
            input_per_1m = max(float(item.get("input_per_1m_usd", 0.0)), 0.0)
            output_per_1m = max(float(item.get("output_per_1m_usd", 0.0)), 0.0)
            cached_value = item.get("cached_input_per_1m_usd")
            cached_per_1m = None if cached_value is None else max(float(cached_value), 0.0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid pricing value in {path}: {item}") from exc

        rates.append(
            BuiltinPricingRate(
                model_pattern=pattern,
                input_per_1m_usd=input_per_1m,
                output_per_1m_usd=output_per_1m,
                cached_input_per_1m_usd=cached_per_1m,
            )
        )

    if not rates:
        raise RuntimeError(f"Pricing catalog has no usable rates: {path}")
    return tuple(rates)


@lru_cache(maxsize=1)
def _builtin_pricing_by_provider() -> dict[str, tuple[BuiltinPricingRate, ...]]:
    """Load built-in provider pricing catalogs from YAML once per process."""

    catalog_dir = _pricing_catalog_dir()
    catalog_by_key: dict[str, tuple[BuiltinPricingRate, ...]] = {}

    for provider_key in sorted(set(_PROVIDER_PRICING_CATALOG_MAP.values())):
        path = catalog_dir / f"{provider_key}.yaml"
        payload = _load_yaml_mapping(path)
        catalog_by_key[provider_key] = _parse_rates_from_payload(path, payload)

    resolved: dict[str, tuple[BuiltinPricingRate, ...]] = {}
    for provider_slug, provider_key in _PROVIDER_PRICING_CATALOG_MAP.items():
        resolved[provider_slug] = catalog_by_key.get(provider_key, tuple())
    return resolved


def list_builtin_pricing_rates() -> list[dict[str, Any]]:
    """Return flattened built-in pricing rows for UI/API reference views."""

    rows: list[dict[str, Any]] = []
    for provider_slug, rates in _builtin_pricing_by_provider().items():
        for rate in rates:
            rows.append(
                {
                    "provider_slug": provider_slug,
                    "model_pattern": normalize_model_pattern(rate.model_pattern),
                    "input_per_1m_usd": rate.input_per_1m_usd,
                    "output_per_1m_usd": rate.output_per_1m_usd,
                    "cached_input_per_1m_usd": rate.cached_input_per_1m_usd,
                }
            )
    rows.sort(key=lambda row: (row["provider_slug"], row["model_pattern"]))
    return rows


def extract_token_usage(usage_json: dict[str, Any] | None) -> TokenUsage:
    """Extract normalized token counters from heterogeneous usage payloads."""

    usage = usage_json or {}
    input_tokens = _to_nonnegative_int(usage.get("input_tokens", usage.get("prompt_tokens", 0)))
    output_tokens = _to_nonnegative_int(usage.get("output_tokens", usage.get("completion_tokens", 0)))

    cached_tokens = 0
    input_details = usage.get("input_tokens_details")
    if isinstance(input_details, dict):
        cached_tokens = _to_nonnegative_int(input_details.get("cached_tokens"))

    if cached_tokens <= 0:
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cached_tokens = _to_nonnegative_int(prompt_details.get("cached_tokens"))

    if cached_tokens <= 0:
        cached_tokens = _to_nonnegative_int(usage.get("cached_tokens", 0))

    if cached_tokens > input_tokens:
        cached_tokens = input_tokens

    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_tokens,
    )


def _resolve_builtin_pricing(
    *,
    provider_slug: str,
    model: str,
) -> ResolvedPricing | None:
    """Resolve best matching built-in pricing row for provider + model."""

    normalized_provider = ensure_supported_provider_slug(provider_slug)
    model_probe = (model or "").strip().lower()
    if not model_probe:
        return None

    rates = _builtin_pricing_by_provider().get(normalized_provider, ())
    if not rates:
        return None

    exact = [rate for rate in rates if normalize_model_pattern(rate.model_pattern) == model_probe]
    if exact:
        selected = exact[0]
        return ResolvedPricing(
            source="builtin_estimate",
            pricing_rate_id=None,
            model_pattern=normalize_model_pattern(selected.model_pattern),
            input_per_1m_usd=selected.input_per_1m_usd,
            output_per_1m_usd=selected.output_per_1m_usd,
            cached_input_per_1m_usd=selected.cached_input_per_1m_usd,
        )

    wildcard_matches = [
        rate
        for rate in rates
        if _is_wildcard_pattern(rate.model_pattern) and fnmatchcase(model_probe, normalize_model_pattern(rate.model_pattern))
    ]
    if not wildcard_matches:
        return None

    wildcard_matches.sort(key=lambda rate: _pattern_specificity(rate.model_pattern), reverse=True)
    selected = wildcard_matches[0]
    return ResolvedPricing(
        source="builtin_estimate",
        pricing_rate_id=None,
        model_pattern=normalize_model_pattern(selected.model_pattern),
        input_per_1m_usd=selected.input_per_1m_usd,
        output_per_1m_usd=selected.output_per_1m_usd,
        cached_input_per_1m_usd=selected.cached_input_per_1m_usd,
    )


def resolve_pricing_rate(
    db: Session,
    *,
    tenant_id: str,
    provider_slug: str,
    model: str,
) -> Any | None:
    """Legacy compatibility helper used by tests/extensions.

    Runtime billing does not use tenant pricing rows in OSS, but this keeps the
    previous wildcard resolution behavior available for compatibility checks.
    """

    del tenant_id  # Tenant-scoped pricing overrides are disabled in OSS runtime.
    normalized_provider = ensure_supported_provider_slug(provider_slug)
    model_probe = (model or "").strip().lower()
    if not model_probe:
        return None

    try:
        candidates = db.scalars(None).all()
    except Exception:  # noqa: BLE001
        return None

    rates = [
        rate
        for rate in candidates
        if getattr(rate, "provider_slug", None) == normalized_provider and bool(getattr(rate, "is_active", True))
    ]
    if not rates:
        return None

    exact = [rate for rate in rates if normalize_model_pattern(getattr(rate, "model_pattern", "")) == model_probe]
    if exact:
        return exact[0]

    wildcard_matches = [
        rate
        for rate in rates
        if _is_wildcard_pattern(getattr(rate, "model_pattern", ""))
        and fnmatchcase(model_probe, normalize_model_pattern(getattr(rate, "model_pattern", "")))
    ]
    if not wildcard_matches:
        return None

    wildcard_matches.sort(
        key=lambda rate: _pattern_specificity(str(getattr(rate, "model_pattern", ""))),
        reverse=True,
    )
    return wildcard_matches[0]


def estimate_job_cost_usd(
    db: Session,
    *,
    tenant_id: str,
    provider_slug: str | None,
    model: str | None,
    usage_json: dict[str, Any] | None,
) -> tuple[float | None, dict[str, Any] | None]:
    """Estimate job cost in USD from usage payload + built-in pricing catalog."""

    del db, tenant_id
    if not provider_slug or not model:
        return None, None

    try:
        resolved = _resolve_builtin_pricing(provider_slug=provider_slug, model=model)
    except ValueError:
        return None, None
    if resolved is None:
        return None, None

    usage = extract_token_usage(usage_json)
    uncached_input_tokens = max(usage.input_tokens - usage.cached_input_tokens, 0)
    cached_rate = (
        resolved.cached_input_per_1m_usd if resolved.cached_input_per_1m_usd is not None else resolved.input_per_1m_usd
    )

    input_cost = (uncached_input_tokens / 1_000_000) * resolved.input_per_1m_usd
    cached_input_cost = (usage.cached_input_tokens / 1_000_000) * cached_rate
    output_cost = (usage.output_tokens / 1_000_000) * resolved.output_per_1m_usd
    total_cost = round(input_cost + cached_input_cost + output_cost, 10)

    details = {
        "pricing_source": resolved.source,
        "pricing_rate_id": resolved.pricing_rate_id,
        "pricing_model_pattern": resolved.model_pattern,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "input_per_1m_usd": resolved.input_per_1m_usd,
        "output_per_1m_usd": resolved.output_per_1m_usd,
        "cached_input_per_1m_usd": cached_rate,
        "estimated_cost_usd": total_cost,
    }
    return total_cost, details
