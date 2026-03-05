"""Provider/model registry loader with strict YAML schema validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.provider_profiles import provider_sort_priority

_PROVIDER_ALLOWED_KEYS = {
    "slug",
    "name",
    "aliases",
    "logo_path",
    "models_from",
    "platform_key_env",
    "requires_api_key",
    "docs",
    "connection_fields",
    "default_model",
    "recommended_models",
}
_PROVIDER_REQUIRED_KEYS = {
    "slug",
    "name",
    "docs",
    "connection_fields",
    "default_model",
    "recommended_models",
    "requires_api_key",
}
_PROVIDER_CONNECTION_FIELD_ALLOWED_KEYS = {"key", "label", "type", "required", "placeholder", "description"}
_MODEL_ALLOWED_KEYS = {
    "model",
    "display_name",
    "family",
    "category",
    "supports_realtime",
    "supports_vision",
    "supports_tools",
    "notes",
    "parameters",
}
_MODEL_REQUIRED_KEYS = {"model", "display_name", "supports_realtime", "supports_vision", "supports_tools", "parameters"}
_MODEL_PARAMETER_ALLOWED_KEYS = {
    "supported",
    "type",
    "min",
    "max",
    "default",
    "values",
    "description",
    "increase_effect",
    "decrease_effect",
}


@dataclass(frozen=True)
class ProviderConnectionField:
    key: str
    label: str
    field_type: str
    required: bool
    placeholder: str | None
    description: str | None


@dataclass(frozen=True)
class ModelParameterSpec:
    key: str
    supported: bool
    param_type: str | None
    min_value: float | int | None
    max_value: float | int | None
    default: Any
    values: tuple[str, ...]
    description: str | None
    increase_effect: str | None
    decrease_effect: str | None


@dataclass(frozen=True)
class ModelSpec:
    provider_slug: str
    model: str
    display_name: str
    family: str | None
    category: str | None
    supports_realtime: bool
    supports_vision: bool
    supports_tools: bool
    notes: str | None
    parameters: tuple[ModelParameterSpec, ...]


@dataclass(frozen=True)
class ProviderSpec:
    slug: str
    name: str
    aliases: tuple[str, ...]
    logo_path: str | None
    models_from: str | None
    platform_key_env: str | None
    requires_api_key: bool
    docs_url: str
    realtime_docs_url: str | None
    connection_fields: tuple[ProviderConnectionField, ...]
    default_model: str
    recommended_models: tuple[str, ...]


@dataclass(frozen=True)
class ModelCompatibilityGroup:
    group_id: str
    title: str
    description: str | None
    members: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ProviderRegistry:
    providers: tuple[ProviderSpec, ...]
    providers_by_slug: dict[str, ProviderSpec]
    models_by_provider: dict[str, tuple[ModelSpec, ...]]
    model_lookup: dict[tuple[str, str], ModelSpec]
    alias_map: dict[str, str]
    compatibility_groups: tuple[ModelCompatibilityGroup, ...]


def _slug_key(value: str) -> str:
    """Return normalized key for alias/provider slug matching."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _normalize_provider_slug(value: str) -> str:
    """Normalize provider slug to canonical snake_case format."""
    return (value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _repo_root() -> Path:
    """Return repository root path."""
    return Path(__file__).resolve().parents[3]


def _providers_dir() -> Path:
    """Return providers registry directory path."""
    return _repo_root() / "providers"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML mapping payload from disk."""
    if not path.exists():
        raise RuntimeError(f"Missing provider registry file: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Registry file must be a mapping: {path}")
    return raw


def _as_tuple_str(values: Any) -> tuple[str, ...]:
    """Normalize list payloads into tuple[str, ...]."""
    if not isinstance(values, list):
        return tuple()
    result: list[str] = []
    for value in values:
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                result.append(cleaned)
    return tuple(result)


def _parse_provider(provider_dir: Path) -> ProviderSpec:
    """Parse one provider.yaml file into ProviderSpec."""
    payload = _load_yaml(provider_dir / "provider.yaml")
    unknown_keys = set(payload) - _PROVIDER_ALLOWED_KEYS
    if unknown_keys:
        raise RuntimeError(
            f"Unsupported keys in {provider_dir / 'provider.yaml'}: {sorted(unknown_keys)}"
        )
    missing_keys = _PROVIDER_REQUIRED_KEYS - set(payload)
    if missing_keys:
        raise RuntimeError(
            f"Missing required keys in {provider_dir / 'provider.yaml'}: {sorted(missing_keys)}"
        )

    slug = _normalize_provider_slug(str(payload.get("slug", "")))
    if not slug:
        raise RuntimeError(f"Provider slug is required: {provider_dir / 'provider.yaml'}")

    name = str(payload.get("name", "")).strip() or slug
    aliases = _as_tuple_str(payload.get("aliases"))
    logo_path = str(payload.get("logo_path", "")).strip() or None
    models_from_raw = str(payload.get("models_from", "")).strip() or None
    models_from = _normalize_provider_slug(models_from_raw) if models_from_raw else None
    if models_from == slug:
        models_from = None
    platform_key_env = str(payload.get("platform_key_env", "")).strip() or None
    requires_api_key = bool(payload.get("requires_api_key", True))

    docs = payload.get("docs") if isinstance(payload.get("docs"), dict) else {}
    docs_url = str(docs.get("api", "")).strip() or ""
    realtime_docs_url = str(docs.get("realtime", "")).strip() or None

    fields_raw = payload.get("connection_fields")
    fields: list[ProviderConnectionField] = []
    if isinstance(fields_raw, list):
        for item in fields_raw:
            if not isinstance(item, dict):
                continue
            unknown_field_keys = set(item) - _PROVIDER_CONNECTION_FIELD_ALLOWED_KEYS
            if unknown_field_keys:
                raise RuntimeError(
                    f"Unsupported connection field keys in {provider_dir / 'provider.yaml'}: {sorted(unknown_field_keys)}"
                )
            key = str(item.get("key", "")).strip()
            if not key:
                continue
            field = ProviderConnectionField(
                key=key,
                label=str(item.get("label", key)).strip() or key,
                field_type=str(item.get("type", "text")).strip() or "text",
                required=bool(item.get("required", False)),
                placeholder=str(item.get("placeholder", "")).strip() or None,
                description=str(item.get("description", "")).strip() or None,
            )
            fields.append(field)

    default_model = str(payload.get("default_model", "")).strip() or "gpt-5-nano"
    recommended_models = _as_tuple_str(payload.get("recommended_models"))

    return ProviderSpec(
        slug=slug,
        name=name,
        aliases=aliases,
        logo_path=logo_path,
        models_from=models_from,
        platform_key_env=platform_key_env,
        requires_api_key=requires_api_key,
        docs_url=docs_url,
        realtime_docs_url=realtime_docs_url,
        connection_fields=tuple(fields),
        default_model=default_model,
        recommended_models=recommended_models,
    )


def _parse_model(provider_slug: str, path: Path) -> ModelSpec:
    """Parse one model YAML file into ModelSpec."""
    payload = _load_yaml(path)
    unknown_keys = set(payload) - _MODEL_ALLOWED_KEYS
    if unknown_keys:
        raise RuntimeError(f"Unsupported keys in {path}: {sorted(unknown_keys)}")
    missing_keys = _MODEL_REQUIRED_KEYS - set(payload)
    if missing_keys:
        raise RuntimeError(f"Missing required keys in {path}: {sorted(missing_keys)}")

    model = str(payload.get("model", "")).strip()
    if not model:
        raise RuntimeError(f"Model id is required: {path}")

    display_name = str(payload.get("display_name", model)).strip() or model
    family = str(payload.get("family", "")).strip() or None
    category = str(payload.get("category", "")).strip() or None
    supports_realtime = bool(payload.get("supports_realtime", False))
    supports_vision = bool(payload.get("supports_vision", False))
    supports_tools = bool(payload.get("supports_tools", False))
    notes = str(payload.get("notes", "")).strip() or None

    params_payload = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    parameters: list[ModelParameterSpec] = []
    for key, raw_spec in params_payload.items():
        if not isinstance(key, str):
            continue
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        unknown_spec_keys = set(spec) - _MODEL_PARAMETER_ALLOWED_KEYS
        if unknown_spec_keys:
            raise RuntimeError(f"Unsupported parameter keys in {path}::{key}: {sorted(unknown_spec_keys)}")
        values = _as_tuple_str(spec.get("values"))
        parameters.append(
            ModelParameterSpec(
                key=key,
                supported=bool(spec.get("supported", True)),
                param_type=str(spec.get("type", "")).strip() or None,
                min_value=spec.get("min"),
                max_value=spec.get("max"),
                default=spec.get("default"),
                values=values,
                description=str(spec.get("description", "")).strip() or None,
                increase_effect=str(spec.get("increase_effect", "")).strip() or None,
                decrease_effect=str(spec.get("decrease_effect", "")).strip() or None,
            )
        )

    return ModelSpec(
        provider_slug=provider_slug,
        model=model,
        display_name=display_name,
        family=family,
        category=category,
        supports_realtime=supports_realtime,
        supports_vision=supports_vision,
        supports_tools=supports_tools,
        notes=notes,
        parameters=tuple(parameters),
    )


def _parse_compatibility(base_dir: Path) -> tuple[ModelCompatibilityGroup, ...]:
    """Parse optional model equivalence/compatibility declarations."""
    path = base_dir / "compatibility" / "model_equivalence.yaml"
    if not path.exists():
        return tuple()

    payload = _load_yaml(path)
    groups_raw = payload.get("groups") if isinstance(payload.get("groups"), list) else []
    groups: list[ModelCompatibilityGroup] = []

    for item in groups_raw:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("id", "")).strip()
        title = str(item.get("title", "")).strip() or group_id
        if not group_id:
            continue
        members_raw = item.get("members") if isinstance(item.get("members"), list) else []
        members: list[tuple[str, str]] = []
        for member in members_raw:
            if not isinstance(member, dict):
                continue
            provider = _normalize_provider_slug(str(member.get("provider", "")))
            model = str(member.get("model", "")).strip()
            if provider and model:
                members.append((provider, model))
        groups.append(
            ModelCompatibilityGroup(
                group_id=group_id,
                title=title,
                description=str(item.get("description", "")).strip() or None,
                members=tuple(members),
            )
        )

    return tuple(groups)


@lru_cache(maxsize=1)
def load_provider_registry() -> ProviderRegistry:
    """Load full provider registry from disk with strict validation."""
    base_dir = _providers_dir()
    if not base_dir.exists():
        raise RuntimeError(f"Providers registry directory not found: {base_dir}")

    providers: list[ProviderSpec] = []
    providers_by_slug: dict[str, ProviderSpec] = {}
    provider_dirs_by_slug: dict[str, Path] = {}
    models_by_provider: dict[str, tuple[ModelSpec, ...]] = {}
    model_lookup: dict[tuple[str, str], ModelSpec] = {}
    alias_map: dict[str, str] = {}

    provider_dirs = sorted([path for path in base_dir.iterdir() if path.is_dir() and (path / "provider.yaml").exists()])
    for provider_dir in provider_dirs:
        provider = _parse_provider(provider_dir)
        if provider.slug in providers_by_slug:
            raise RuntimeError(f"Duplicate provider slug '{provider.slug}' in {provider_dir}")
        providers.append(provider)
        providers_by_slug[provider.slug] = provider
        provider_dirs_by_slug[provider.slug] = provider_dir

        alias_map[_slug_key(provider.slug)] = provider.slug
        for alias in provider.aliases:
            alias_map[_slug_key(alias)] = provider.slug

    for provider in providers:
        source_slug = provider.models_from or provider.slug
        source_dir = provider_dirs_by_slug.get(source_slug)
        if source_dir is None:
            raise RuntimeError(
                f"Provider {provider.slug} declares models_from='{source_slug}', but source provider was not found."
            )

        model_files = sorted((source_dir / "models").glob("*.yaml"))
        provider_models: list[ModelSpec] = []
        for model_file in model_files:
            model_spec = _parse_model(provider.slug, model_file)
            provider_models.append(model_spec)
            model_lookup[(provider.slug, model_spec.model)] = model_spec

        if not provider_models:
            raise RuntimeError(f"Provider {provider.slug} has no model declarations in {source_dir / 'models'}")
        models_by_provider[provider.slug] = tuple(provider_models)

    compatibility_groups = _parse_compatibility(base_dir)

    # Ensure deterministic ordering for UI.
    providers.sort(key=lambda item: (provider_sort_priority(item.slug), item.slug))

    return ProviderRegistry(
        providers=tuple(providers),
        providers_by_slug=providers_by_slug,
        models_by_provider=models_by_provider,
        model_lookup=model_lookup,
        alias_map=alias_map,
        compatibility_groups=compatibility_groups,
    )


def clear_provider_registry_cache() -> None:
    """Clear in-process provider registry cache."""
    load_provider_registry.cache_clear()


def normalize_provider_slug(value: str) -> str:
    """Normalize provider slug and resolve known aliases."""
    registry = load_provider_registry()
    cleaned = (value or "").strip()
    if not cleaned:
        # Keep historical default behavior.
        return "openai"

    normalized = _normalize_provider_slug(cleaned)
    if normalized in registry.providers_by_slug:
        return normalized

    alias = registry.alias_map.get(_slug_key(cleaned))
    if alias:
        return alias

    return normalized


def ensure_supported_provider_slug(value: str) -> str:
    """Normalize provider slug and assert it exists in the registry."""
    normalized = normalize_provider_slug(value)
    registry = load_provider_registry()
    if normalized not in registry.providers_by_slug:
        supported = ", ".join(sorted(registry.providers_by_slug))
        raise ValueError(f"Unsupported provider '{value}'. Supported providers: {supported}")
    return normalized


def list_provider_specs() -> list[ProviderSpec]:
    """Return provider catalog specs in deterministic UI order."""
    return list(load_provider_registry().providers)


def get_provider_spec(provider_slug: str) -> ProviderSpec | None:
    """Return one provider spec by slug/alias."""
    normalized = normalize_provider_slug(provider_slug)
    return load_provider_registry().providers_by_slug.get(normalized)


def list_models_for_provider(provider_slug: str) -> list[ModelSpec]:
    """Return declared model specs for one provider."""
    normalized = ensure_supported_provider_slug(provider_slug)
    return list(load_provider_registry().models_by_provider.get(normalized, tuple()))


def get_model_spec(provider_slug: str, model: str) -> ModelSpec | None:
    """Return model spec by provider + model id."""
    normalized = normalize_provider_slug(provider_slug)
    return load_provider_registry().model_lookup.get((normalized, (model or "").strip()))


def equivalent_models(provider_slug: str, model: str) -> list[tuple[str, str]]:
    """Return equivalent provider/model routes from compatibility groups."""
    registry = load_provider_registry()
    normalized_provider = normalize_provider_slug(provider_slug)
    normalized_model = (model or "").strip()
    if not normalized_provider or not normalized_model:
        return []

    provider_spec = registry.providers_by_slug.get(normalized_provider)
    canonical_provider = provider_spec.models_from if provider_spec and provider_spec.models_from else normalized_provider

    matches: list[tuple[str, str]] = []
    for group in registry.compatibility_groups:
        members = list(group.members)
        if (normalized_provider, normalized_model) not in members and (canonical_provider, normalized_model) not in members:
            continue
        for member_provider, member_model in members:
            if (member_provider, member_model) in {
                (normalized_provider, normalized_model),
                (canonical_provider, normalized_model),
            }:
                continue
            matches.append((member_provider, member_model))
            # If this compatibility group references a canonical provider with
            # profile variants, include those variants as equivalent too.
            for provider in registry.providers:
                if provider.models_from != member_provider:
                    continue
                if (provider.slug, member_model) in registry.model_lookup:
                    matches.append((provider.slug, member_model))

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in matches:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped
