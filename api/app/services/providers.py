"""Provider connection management + credential resolution.

This service is the single source of truth for:
- persisting tenant provider connections
- resolving inherited provider configs across nested tenants
- normalizing provider endpoint options (especially Azure variants)
- loading runtime credentials for model execution
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.provider_catalog import (
    ProviderCatalogItem,
    ensure_supported_provider_slug,
    get_provider_catalog_item,
    list_provider_catalog,
)
from app.core.provider_profiles import azure_provider_mode, is_azure_provider_slug
from app.models import LlmAuthMode, ProviderAuthMode, ProviderBillingMode, Tenant, TenantProviderConfig
from app.services.tenant_secrets import delete_secret, get_secret, put_secret


@dataclass(frozen=True)
class ResolvedProviderCredentials:
    provider_slug: str
    model_prefix: str
    auth_mode: ProviderAuthMode
    api_key: str | None
    api_base: str | None
    api_version: str | None
    extra_json: dict[str, Any]
    source_tenant_id: str | None = None
    provider_config_id: str | None = None
    provider_connection_name: str | None = None


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.lower() in {"none", "null"}:
        return None
    return cleaned


def _auth_mode_from_billing_mode(_: ProviderBillingMode) -> ProviderAuthMode:
    # OSS runtime enforces BYOK-only billing. Auth mode is configured independently.
    return ProviderAuthMode.tenant


def _billing_mode_from_auth_mode(_: ProviderAuthMode) -> ProviderBillingMode:
    return ProviderBillingMode.byok


def build_provider_secret_ref(tenant_id: str, provider_slug: str, connection_id: str | None = None) -> str:
    normalized = ensure_supported_provider_slug(provider_slug)
    if connection_id:
        return f"tenant:{tenant_id}:provider:{normalized}:connection:{connection_id}:api_key"
    return f"tenant:{tenant_id}:provider:{normalized}:api_key"


def _platform_provider_key_map() -> dict[str, str]:
    raw = (get_settings().platform_provider_keys_json or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}

    mapped: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        try:
            normalized = ensure_supported_provider_slug(key)
        except ValueError:
            continue
        token = value.strip()
        if token:
            mapped[normalized] = token
    return mapped


def platform_key_for_provider(provider_slug: str) -> str | None:
    normalized = ensure_supported_provider_slug(provider_slug)

    platform_map = _platform_provider_key_map()
    mapped = platform_map.get(normalized)
    if not mapped and is_azure_provider_slug(normalized):
        mapped = platform_map.get("azure_openai")
    if mapped:
        return mapped

    provider = get_provider_catalog_item(normalized)
    env_hint = provider.platform_key_env if provider else None
    if env_hint:
        value = os.getenv(env_hint)
        if value:
            return value.strip()

    if is_azure_provider_slug(normalized):
        fallback = os.getenv("AZURE_OPENAI_API_KEY")
        if fallback:
            return fallback.strip()

    return None


def platform_key_available(provider_slug: str) -> bool:
    return bool(platform_key_for_provider(provider_slug))


def _config_ordering(stmt):
    return stmt.order_by(
        TenantProviderConfig.is_default.desc(),
        TenantProviderConfig.is_active.desc(),
        TenantProviderConfig.updated_at.desc(),
        TenantProviderConfig.created_at.desc(),
    )


def get_tenant_provider_config_by_id(db: Session, tenant_id: str, provider_config_id: str) -> TenantProviderConfig | None:
    return db.scalar(
        select(TenantProviderConfig).where(
            TenantProviderConfig.id == provider_config_id,
            TenantProviderConfig.tenant_id == tenant_id,
        )
    )


def get_tenant_provider_config(db: Session, tenant_id: str, provider_slug: str) -> TenantProviderConfig | None:
    normalized = ensure_supported_provider_slug(provider_slug)
    stmt = select(TenantProviderConfig).where(
        TenantProviderConfig.tenant_id == tenant_id,
        TenantProviderConfig.provider_slug == normalized,
    )
    return db.scalar(_config_ordering(stmt))


def list_tenant_provider_configs(
    db: Session,
    tenant_id: str,
    provider_slug: str | None = None,
) -> list[TenantProviderConfig]:
    stmt = select(TenantProviderConfig).where(TenantProviderConfig.tenant_id == tenant_id)
    if provider_slug:
        stmt = stmt.where(TenantProviderConfig.provider_slug == ensure_supported_provider_slug(provider_slug))
    return db.scalars(
        stmt.order_by(
            TenantProviderConfig.provider_slug.asc(),
            TenantProviderConfig.is_default.desc(),
            TenantProviderConfig.name.asc(),
        )
    ).all()


def list_effective_provider_configs(
    db: Session,
    *,
    tenant_id: str,
    provider_slug: str,
) -> tuple[list[TenantProviderConfig], Tenant | None]:
    normalized = ensure_supported_provider_slug(provider_slug)
    current_tenant = db.get(Tenant, tenant_id)
    depth = 0
    max_depth = max(int(get_settings().tenant_hierarchy_max_depth), 1)

    while current_tenant is not None and depth < max_depth:
        stmt = select(TenantProviderConfig).where(
            TenantProviderConfig.tenant_id == current_tenant.id,
            TenantProviderConfig.provider_slug == normalized,
        )
        configs = db.scalars(_config_ordering(stmt)).all()
        if configs:
            return configs, current_tenant

        if not current_tenant.inherit_provider_configs or not current_tenant.parent_tenant_id:
            return [], current_tenant

        current_tenant = db.get(Tenant, current_tenant.parent_tenant_id)
        depth += 1

    return [], current_tenant


def get_effective_provider_config(
    db: Session,
    *,
    tenant_id: str,
    provider_slug: str,
) -> tuple[TenantProviderConfig | None, Tenant | None]:
    configs, source_tenant = list_effective_provider_configs(db, tenant_id=tenant_id, provider_slug=provider_slug)
    return (configs[0] if configs else None), source_tenant


def get_effective_provider_config_by_id(
    db: Session,
    *,
    tenant_id: str,
    provider_config_id: str,
) -> tuple[TenantProviderConfig | None, Tenant | None]:
    current_tenant = db.get(Tenant, tenant_id)
    depth = 0
    max_depth = max(int(get_settings().tenant_hierarchy_max_depth), 1)

    while current_tenant is not None and depth < max_depth:
        config = db.scalar(
            select(TenantProviderConfig).where(
                TenantProviderConfig.id == provider_config_id,
                TenantProviderConfig.tenant_id == current_tenant.id,
            )
        )
        if config is not None:
            return config, current_tenant

        if not current_tenant.inherit_provider_configs or not current_tenant.parent_tenant_id:
            return None, current_tenant

        current_tenant = db.get(Tenant, current_tenant.parent_tenant_id)
        depth += 1

    return None, current_tenant


def has_tenant_key(config: TenantProviderConfig) -> bool:
    if not config.key_ref:
        return False
    try:
        return bool(get_secret(config.key_ref))
    except RuntimeError:
        return False


def _new_connection_name(db: Session, tenant_id: str, provider_slug: str) -> str:
    provider = get_provider_catalog_item(provider_slug)
    base_name = provider.name if provider else provider_slug.replace("_", " ").title()
    existing_names = {
        item.name.strip().lower()
        for item in list_tenant_provider_configs(db, tenant_id=tenant_id, provider_slug=provider_slug)
        if item.name
    }
    if base_name.strip().lower() not in existing_names:
        return base_name

    suffix = 2
    while True:
        candidate = f"{base_name} {suffix}"
        if candidate.strip().lower() not in existing_names:
            return candidate
        suffix += 1


def upsert_tenant_provider_config(
    db: Session,
    *,
    tenant_id: str,
    provider_slug: str,
    provider_config_id: str | None = None,
    connection_name: str | None = None,
    description: str | None = None,
    billing_mode: str | None = None,
    auth_mode: str | None = None,
    api_key: str | None = None,
    clear_api_key: bool = False,
    api_base: str | None = None,
    api_version: str | None = None,
    extra_json: dict[str, Any] | None = None,
    is_active: bool | None = None,
    is_default: bool | None = None,
) -> TenantProviderConfig:
    normalized = ensure_supported_provider_slug(provider_slug)

    config: TenantProviderConfig | None = None
    if provider_config_id:
        config = get_tenant_provider_config_by_id(db, tenant_id, provider_config_id)
        if config is None:
            raise RuntimeError("Provider connection not found")
        if config.provider_slug != normalized:
            raise RuntimeError("Provider connection does not match selected provider")

    if config is None:
        name = (connection_name or "").strip() or _new_connection_name(db, tenant_id, normalized)
        has_existing = bool(list_tenant_provider_configs(db, tenant_id=tenant_id, provider_slug=normalized))
        config = TenantProviderConfig(
            tenant_id=tenant_id,
            provider_slug=normalized,
            name=name,
            description=_normalize_optional_text(description),
            billing_mode=ProviderBillingMode.byok,
            auth_mode=ProviderAuthMode.tenant,
            extra_json={},
            is_active=True,
            is_default=not has_existing,
        )
        db.add(config)
        db.commit()
        db.refresh(config)

    incoming_name = (connection_name or "").strip()
    if incoming_name:
        config.name = incoming_name
    if description is not None:
        config.description = _normalize_optional_text(description)

    if clear_api_key and config.key_ref:
        delete_secret(config.key_ref)
        config.key_ref = None

    incoming_key = (api_key or "").strip()
    if incoming_key:
        secret_ref = config.key_ref or build_provider_secret_ref(tenant_id, normalized, connection_id=config.id)
        put_secret(secret_ref, incoming_key)
        config.key_ref = secret_ref

    if billing_mode is not None:
        resolved_billing_mode = ProviderBillingMode(billing_mode)
        if resolved_billing_mode != ProviderBillingMode.byok:
            raise RuntimeError("Only 'byok' billing_mode is supported in OSS runtime")
        config.billing_mode = ProviderBillingMode.byok
        if auth_mode is None:
            config.auth_mode = _auth_mode_from_billing_mode(resolved_billing_mode)
    if auth_mode is not None:
        resolved_auth_mode = ProviderAuthMode(auth_mode)
        config.auth_mode = resolved_auth_mode
        config.billing_mode = _billing_mode_from_auth_mode(resolved_auth_mode)
    elif billing_mode is None:
        config.billing_mode = ProviderBillingMode.byok

    if api_base is not None:
        config.api_base = _normalize_optional_text(api_base)
    if api_version is not None:
        config.api_version = _normalize_optional_text(api_version)
    if extra_json is not None:
        config.extra_json = extra_json
    if is_active is not None:
        config.is_active = is_active

    if is_default is not None:
        config.is_default = is_default

    db.add(config)
    db.commit()
    db.refresh(config)

    if config.is_default:
        db.query(TenantProviderConfig).filter(
            and_(
                TenantProviderConfig.tenant_id == tenant_id,
                TenantProviderConfig.provider_slug == normalized,
                TenantProviderConfig.id != config.id,
            )
        ).update({"is_default": False}, synchronize_session=False)
        db.commit()
        db.refresh(config)

    return config


def delete_tenant_provider_config(
    db: Session,
    *,
    tenant_id: str,
    provider_slug: str,
    provider_config_id: str | None = None,
) -> bool:
    normalized = ensure_supported_provider_slug(provider_slug)

    config: TenantProviderConfig | None = None
    if provider_config_id:
        config = get_tenant_provider_config_by_id(db, tenant_id, provider_config_id)
        if config is not None and config.provider_slug != normalized:
            config = None
    else:
        config = get_tenant_provider_config(db, tenant_id, normalized)

    if config is None:
        return False

    if config.key_ref:
        delete_secret(config.key_ref)

    was_default = config.is_default
    db.delete(config)
    db.commit()

    if was_default:
        next_config = db.scalar(
            _config_ordering(
                select(TenantProviderConfig).where(
                    TenantProviderConfig.tenant_id == tenant_id,
                    TenantProviderConfig.provider_slug == normalized,
                )
            )
        )
        if next_config is not None:
            next_config.is_default = True
            db.add(next_config)
            db.commit()

    return True


def provider_catalog_for_tenant(
    db: Session,
    tenant_id: str,
) -> list[tuple[ProviderCatalogItem, TenantProviderConfig | None, bool, bool]]:
    result: list[tuple[ProviderCatalogItem, TenantProviderConfig | None, bool, bool]] = []
    for provider in list_provider_catalog():
        config, _ = get_effective_provider_config(db, tenant_id=tenant_id, provider_slug=provider.slug)
        configured = has_tenant_key(config) if config else False
        platform_available = platform_key_available(provider.slug)
        result.append((provider, config, configured, platform_available))
    return result


def provider_config_is_ready(config: TenantProviderConfig | None) -> bool:
    if config is None or not config.is_active:
        return False

    if config.auth_mode == ProviderAuthMode.tenant:
        return has_tenant_key(config)
    if config.auth_mode == ProviderAuthMode.platform:
        return platform_key_available(config.provider_slug)
    if config.auth_mode == ProviderAuthMode.none:
        provider = get_provider_catalog_item(config.provider_slug)
        return not bool(provider.requires_api_key if provider else True)
    return False


def list_ready_provider_catalog_for_tenant(db: Session, tenant_id: str) -> list[ProviderCatalogItem]:
    ready: list[ProviderCatalogItem] = []
    for provider in list_provider_catalog():
        configs, _ = list_effective_provider_configs(db, tenant_id=tenant_id, provider_slug=provider.slug)
        if any(provider_config_is_ready(config) for config in configs):
            ready.append(provider)
    return ready


def list_ready_provider_connections_for_tenant(
    db: Session,
    tenant_id: str,
    provider_slug: str | None = None,
) -> list[TenantProviderConfig]:
    if provider_slug:
        normalized = ensure_supported_provider_slug(provider_slug)
        configs, _ = list_effective_provider_configs(db, tenant_id=tenant_id, provider_slug=normalized)
        return [config for config in configs if provider_config_is_ready(config)]

    ready: list[TenantProviderConfig] = []
    for provider in list_provider_catalog():
        configs, _ = list_effective_provider_configs(db, tenant_id=tenant_id, provider_slug=provider.slug)
        ready.extend([config for config in configs if provider_config_is_ready(config)])
    return ready


def _resolve_legacy_openai_key(tenant: Tenant) -> tuple[ProviderAuthMode, str | None]:
    if tenant.llm_auth_mode == LlmAuthMode.tenant:
        if not tenant.openai_key_ref:
            return ProviderAuthMode.tenant, None
        return ProviderAuthMode.tenant, get_secret(tenant.openai_key_ref)
    return ProviderAuthMode.platform, platform_key_for_provider("openai")


def _normalize_openai_compatible_base(api_base: str | None, *, force_openai_v1_mode: bool = False) -> str | None:
    raw = (api_base or "").strip()
    if not raw:
        return None

    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.rstrip("/")

    host = (parsed.netloc or "").lower().split(":", 1)[0]
    path = parsed.path.rstrip("/")
    path_lower = path.lower()

    # Azure AI Foundry / shared inference hosts are OpenAI-compatible and
    # expect /openai/v1 model-id mode (not deployment-route mode).
    force_openai_v1_mode = force_openai_v1_mode or host.endswith("llm-inference.openai.azure.com")

    # Canonical OpenAI-compatible Azure path.
    if "/openai/v1" in path_lower:
        start = path_lower.find("/openai/v1")
        normalized_path = path[start : start + len("/openai/v1")]
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
    # If users paste full legacy REST URLs (deployments/chat/responses/etc),
    # reduce to resource root. Azure client builds request paths itself.
    elif "/openai/deployments" in path_lower:
        if force_openai_v1_mode:
            normalized_path = "/openai/v1"
        else:
            marker_idx = path_lower.find("/openai/deployments")
            normalized_path = path[:marker_idx]
    elif "/chat/completions" in path_lower or "/responses" in path_lower or "/embeddings" in path_lower:
        if force_openai_v1_mode:
            normalized_path = "/openai/v1"
        else:
            marker_idx = path_lower.find("/openai")
            normalized_path = path[:marker_idx] if marker_idx >= 0 else path
    elif path_lower in {"", "/"}:
        normalized_path = "/openai/v1" if force_openai_v1_mode else ""
    elif path_lower == "/openai":
        normalized_path = "/openai/v1" if force_openai_v1_mode else ""
    else:
        normalized_path = "/openai/v1" if force_openai_v1_mode else path

    normalized_path = normalized_path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


def _normalize_azure_resource_base(api_base: str | None) -> str | None:
    raw = (api_base or "").strip()
    if not raw:
        return None

    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.rstrip("/")

    path = parsed.path.rstrip("/")
    path_lower = path.lower()
    marker_idx = -1
    for marker in ("/openai/deployments", "/openai/v1", "/openai", "/chat/completions", "/responses", "/embeddings"):
        idx = path_lower.find(marker)
        if idx >= 0:
            marker_idx = idx
            break
    if marker_idx >= 0:
        path = path[:marker_idx]

    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


def _normalize_azure_foundry_base(api_base: str | None) -> str | None:
    raw = (api_base or "").strip()
    if not raw:
        return None

    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.rstrip("/")

    host = (parsed.netloc or "").lower().split(":", 1)[0]
    path = parsed.path.rstrip("/")
    path_lower = path.lower()

    # Azure AI Foundry model inference endpoint family.
    if host.endswith("services.ai.azure.com"):
        if "/models" in path_lower:
            idx = path_lower.find("/models")
            path = path[idx : idx + len("/models")]
        else:
            path = "/models"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    # Fallback to OpenAI-compatible v1 behavior for compatible Azure hosts.
    return _normalize_openai_compatible_base(raw, force_openai_v1_mode=True)


def _extract_api_version_from_base_url(api_base: str | None) -> str | None:
    raw = (api_base or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if not parsed.query:
        return None
    query = parse_qs(parsed.query)
    for key in ("api-version", "api_version"):
        values = query.get(key)
        if not values:
            continue
        value = (values[0] or "").strip()
        if value:
            return value
    return None


def _platform_endpoint_defaults(provider_slug: str) -> tuple[str | None, str | None]:
    if is_azure_provider_slug(provider_slug):
        mode = azure_provider_mode(provider_slug) or "auto"

        default_base = (
            (os.getenv("AZURE_OPENAI_BASE_URL") or "").strip()
            or (os.getenv("AZURE_OPENAI_ENDPOINT") or "").strip()
            or None
        )
        default_version = (os.getenv("AZURE_OPENAI_API_VERSION") or "").strip() or None

        if mode == "deployment":
            api_base = _normalize_azure_resource_base(default_base)
            api_version = default_version or "2024-10-21"
            return api_base, api_version

        if mode == "openai_v1":
            api_base = _normalize_openai_compatible_base(default_base, force_openai_v1_mode=True)
            return api_base, None

        if mode == "foundry":
            foundry_base = (
                (os.getenv("AZURE_AI_FOUNDRY_BASE_URL") or "").strip()
                or (os.getenv("AZURE_AI_FOUNDRY_ENDPOINT") or "").strip()
                or default_base
            )
            foundry_version = (os.getenv("AZURE_AI_FOUNDRY_API_VERSION") or "").strip() or None
            api_base = _normalize_azure_foundry_base(foundry_base)
            api_version = foundry_version or default_version
            if api_base and "/openai/v1" in api_base:
                api_version = None
            return api_base, api_version

        # auto
        api_base = _normalize_openai_compatible_base(default_base)
        if default_version is not None:
            api_version = default_version
        elif api_base and "/openai/v1" in api_base:
            api_version = None
        else:
            api_version = "2024-10-21"
        return api_base, api_version

    return None, None


def resolve_provider_endpoint_options(
    provider_slug: str,
    *,
    api_base: str | None = None,
    api_version: str | None = None,
    use_platform_defaults: bool = True,
) -> tuple[str | None, str | None]:
    normalized = ensure_supported_provider_slug(provider_slug)
    resolved_base = _normalize_optional_text(api_base)
    resolved_version = _normalize_optional_text(api_version)
    extracted_version = _extract_api_version_from_base_url(resolved_base)

    if use_platform_defaults:
        default_base, default_version = _platform_endpoint_defaults(normalized)
        if not resolved_base and default_base:
            resolved_base = default_base
        if not resolved_version and default_version:
            resolved_version = default_version

    if is_azure_provider_slug(normalized):
        mode = azure_provider_mode(normalized) or "auto"

        if mode == "deployment":
            resolved_base = _normalize_azure_resource_base(resolved_base)
            if not resolved_version and extracted_version:
                resolved_version = extracted_version
            if not resolved_version:
                resolved_version = "2024-10-21"
        elif mode == "openai_v1":
            resolved_base = _normalize_openai_compatible_base(resolved_base, force_openai_v1_mode=True)
            resolved_version = None
        elif mode == "foundry":
            resolved_base = _normalize_azure_foundry_base(resolved_base)
            if resolved_base and "/openai/v1" in resolved_base:
                resolved_version = None
            elif not resolved_version and extracted_version:
                resolved_version = extracted_version
        else:
            resolved_base = _normalize_openai_compatible_base(resolved_base)
            if not resolved_version and extracted_version:
                resolved_version = extracted_version
            if resolved_base and "/openai/v1" in resolved_base:
                resolved_version = None

    return resolved_base, resolved_version


def resolve_provider_credentials(
    db: Session,
    *,
    tenant_id: str,
    provider_slug: str,
    provider_config_id: str | None = None,
) -> ResolvedProviderCredentials:
    normalized = ensure_supported_provider_slug(provider_slug)
    provider = get_provider_catalog_item(normalized)
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise RuntimeError("Tenant not found")

    config: TenantProviderConfig | None = None
    source_tenant: Tenant | None = None
    if provider_config_id:
        config, source_tenant = get_effective_provider_config_by_id(
            db,
            tenant_id=tenant_id,
            provider_config_id=provider_config_id,
        )
        if config is None:
            raise RuntimeError("Provider connection not found")
        if config.provider_slug != normalized:
            raise RuntimeError("Provider connection mismatch")
    else:
        config, source_tenant = get_effective_provider_config(db, tenant_id=tenant_id, provider_slug=normalized)

    auth_mode = ProviderAuthMode.platform
    api_key: str | None = None
    api_base: str | None = None
    api_version: str | None = None
    extra_json: dict[str, Any] = {}

    if config is not None:
        if not config.is_active:
            raise RuntimeError(f"Provider '{normalized}' connection '{config.name}' is disabled")

        auth_mode = config.auth_mode
        api_base = config.api_base
        api_version = config.api_version
        extra_json = config.extra_json or {}

        if auth_mode == ProviderAuthMode.tenant:
            if not config.key_ref:
                raise RuntimeError(
                    f"Provider '{normalized}' connection '{config.name}' is tenant-auth but has no key configured"
                )
            api_key = get_secret(config.key_ref)
        elif auth_mode == ProviderAuthMode.platform:
            api_key = platform_key_for_provider(normalized)
    else:
        # Backward compatibility for legacy tenant OpenAI mode.
        if normalized == "openai":
            current_tenant: Tenant | None = tenant
            depth = 0
            max_depth = max(int(get_settings().tenant_hierarchy_max_depth), 1)
            while current_tenant is not None and depth < max_depth:
                auth_mode, api_key = _resolve_legacy_openai_key(current_tenant)
                if api_key:
                    source_tenant = current_tenant
                    break
                if not current_tenant.inherit_provider_configs or not current_tenant.parent_tenant_id:
                    break
                current_tenant = db.get(Tenant, current_tenant.parent_tenant_id)
                depth += 1
        else:
            auth_mode = ProviderAuthMode.platform
            api_key = platform_key_for_provider(normalized)

    api_base, api_version = resolve_provider_endpoint_options(
        normalized,
        api_base=api_base,
        api_version=api_version,
    )

    if provider and provider.requires_api_key and auth_mode != ProviderAuthMode.none and not api_key:
        raise RuntimeError(f"No API key available for provider '{normalized}'")

    if auth_mode == ProviderAuthMode.none:
        api_key = None

    return ResolvedProviderCredentials(
        provider_slug=normalized,
        model_prefix=provider.model_prefix if provider else normalized,
        auth_mode=auth_mode,
        api_key=api_key,
        api_base=api_base,
        api_version=api_version,
        extra_json=extra_json,
        source_tenant_id=source_tenant.id if source_tenant else tenant_id,
        provider_config_id=config.id if config else None,
        provider_connection_name=config.name if config else None,
    )
