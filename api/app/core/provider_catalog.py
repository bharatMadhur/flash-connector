"""Read-only provider catalog helpers built from registry YAML files."""

from dataclasses import dataclass

from app.core.provider_registry import (
    ensure_supported_provider_slug as ensure_provider_slug,
    equivalent_models as resolve_equivalent_models,
    get_model_spec,
    get_provider_spec,
    list_models_for_provider,
    list_provider_specs,
    normalize_provider_slug as normalize_provider,
)


@dataclass(frozen=True)
class ProviderCatalogItem:
    """Provider metadata used by UI, validation, and runtime defaults."""

    slug: str
    name: str
    logo_path: str | None
    model_prefix: str
    default_model: str
    recommended_models: tuple[str, ...]
    platform_key_env: str | None
    requires_api_key: bool
    docs_url: str
    realtime_docs_url: str | None


def _catalog_item_from_registry(slug: str) -> ProviderCatalogItem | None:
    """Map registry provider spec to ProviderCatalogItem."""
    provider = get_provider_spec(slug)
    if provider is None:
        return None
    return ProviderCatalogItem(
        slug=provider.slug,
        name=provider.name,
        logo_path=provider.logo_path,
        model_prefix=provider.slug,
        default_model=provider.default_model,
        recommended_models=provider.recommended_models,
        platform_key_env=provider.platform_key_env,
        requires_api_key=provider.requires_api_key,
        docs_url=provider.docs_url,
        realtime_docs_url=provider.realtime_docs_url,
    )


def normalize_provider_slug(value: str) -> str:
    """Normalize provider slug and aliases to canonical slug."""
    return normalize_provider(value)


def ensure_supported_provider_slug(value: str) -> str:
    """Normalize provider slug and require it exists in registry."""
    return ensure_provider_slug(value)


def get_provider_catalog_item(provider_slug: str) -> ProviderCatalogItem | None:
    """Return one catalog provider item by slug/alias."""
    normalized = normalize_provider_slug(provider_slug)
    return _catalog_item_from_registry(normalized)


def list_provider_catalog() -> list[ProviderCatalogItem]:
    """Return provider catalog items in deterministic registry order."""
    items: list[ProviderCatalogItem] = []
    for provider in list_provider_specs():
        item = _catalog_item_from_registry(provider.slug)
        if item is not None:
            items.append(item)
    return items


def list_provider_models(provider_slug: str) -> list[str]:
    """Return provider models with recommended models prioritized first."""
    normalized = ensure_supported_provider_slug(provider_slug)
    provider_item = get_provider_catalog_item(normalized)
    models = [item.model for item in list_models_for_provider(normalized)]
    if provider_item is None:
        return models

    preferred = [model for model in provider_item.recommended_models if model in models]
    preferred_set = set(preferred)
    remainder = [model for model in models if model not in preferred_set]
    return [*preferred, *remainder]


def get_model_parameters(provider_slug: str, model: str) -> list[dict]:
    """Return declared model parameter metadata for UI form generation."""
    spec = get_model_spec(provider_slug, model)
    if spec is None:
        return []
    params: list[dict] = []
    for item in spec.parameters:
        params.append(
            {
                "key": item.key,
                "supported": item.supported,
                "type": item.param_type,
                "min": item.min_value,
                "max": item.max_value,
                "default": item.default,
                "values": list(item.values),
                "description": item.description,
                "increase_effect": item.increase_effect,
                "decrease_effect": item.decrease_effect,
            }
        )
    return params


def list_equivalent_models(provider_slug: str, model: str) -> list[dict[str, str]]:
    """Return compatible cross-provider model routes for one model."""
    return [
        {"provider": provider, "model": model_name}
        for provider, model_name in resolve_equivalent_models(provider_slug, model)
    ]
