"""Session-authenticated JSON admin API routes for tenant control plane."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.provider_catalog import (
    ensure_supported_provider_slug,
    get_provider_catalog_item,
    list_provider_catalog,
)
from app.core.provider_registry import get_provider_spec
from app.dependencies import SessionUser, csrf_protect, get_session_user, require_roles
from app.core.security import hash_password
from app.models import (
    ApiKey,
    ContextBlock,
    Endpoint,
    EndpointVersion,
    EndpointVersionContext,
    Job,
    JobStatus,
    LlmAuthMode,
    Persona,
    PortalLink,
    Target,
    Tenant,
    TenantProviderConfig,
    TenantVariable,
    TrainingEvent,
    User,
    UserRole,
)
from app.schemas.api_keys import ApiKeyCreate, ApiKeyCreateOut, ApiKeyOut
from app.schemas.endpoints import (
    ActivateVersionRequest,
    EndpointCreate,
    EndpointOut,
    EndpointUpdate,
    EndpointVersionCreate,
    EndpointVersionOut,
    PromptUpdateRequest,
)
from app.schemas.jobs import JobOut
from app.schemas.pricing import BuiltinPricingRateOut
from app.schemas.portal import PortalLinkCreate, PortalLinkCreateOut, PortalLinkOut
from app.schemas.providers import (
    ProviderCatalogOut,
    ProviderConfigCreate,
    ProviderConfigOut,
    ProviderConfigUpdate,
)
from app.schemas.studio import (
    ContextBlockCreate,
    ContextBlockOut,
    ContextBlockUpdate,
    PersonaCreate,
    PersonaOut,
    PersonaUpdate,
    TenantVariableCreate,
    TenantVariableOut,
    TenantVariableUpdate,
)
from app.schemas.tenants import (
    TenantCreate,
    TenantLLMSettingsOut,
    TenantLLMSettingsUpdate,
    TenantOut,
    TenantUpdate,
)
from app.schemas.targets import TargetCreate, TargetOut, TargetUpdate, TargetVerifyResponse
from app.schemas.training import TrainingEventOut, TrainingExportRequest
from app.schemas.users import UserCreate, UserOut, UserPasswordUpdate, UserUpdate
from app.schemas.usage import UsageBucketOut, UsageSummaryOut
from app.services.api_keys import create_virtual_key
from app.services.audit import log_action
from app.services.portal import create_portal_link, get_portal_link, list_portal_links, revoke_portal_link
from app.services.provider_validation import validate_provider_api_key
from app.services.pricing import list_builtin_pricing_rates
from app.services.providers import (
    delete_tenant_provider_config,
    get_tenant_provider_config,
    get_tenant_provider_config_by_id,
    has_tenant_key,
    list_tenant_provider_configs,
    platform_key_available,
    resolve_provider_endpoint_options,
    upsert_tenant_provider_config,
)
from app.services.tenant_llm import build_openai_secret_ref, tenant_has_configured_key
from app.services.tenant_secrets import delete_secret, put_secret
from app.services.tenants import is_same_or_descendant, list_accessible_tenants
from app.services.targets import (
    create_target_record,
    delete_target_record,
    get_target,
    list_targets,
    update_target_record,
    verify_target,
)
from app.services.training import export_training_jsonl, query_training_events
from app.services.usage_costs import UsageSummary, build_usage_summary
from app.services.versioning import create_endpoint_version_record

router = APIRouter(tags=["admin"], dependencies=[Depends(csrf_protect)])


def _usage_summary_out(summary: UsageSummary) -> UsageSummaryOut:
    return UsageSummaryOut(
        window_hours=summary.window_hours,
        from_at=summary.from_at,
        to_at=summary.to_at,
        jobs_total=summary.jobs_total,
        jobs_completed=summary.jobs_completed,
        jobs_failed=summary.jobs_failed,
        jobs_canceled=summary.jobs_canceled,
        estimated_cost_usd=summary.estimated_cost_usd,
        byok_cost_usd=summary.byok_cost_usd,
        input_tokens=summary.input_tokens,
        output_tokens=summary.output_tokens,
        total_tokens=summary.total_tokens,
        by_billing_mode=[
            UsageBucketOut(
                key=item.key,
                label=item.label,
                jobs_total=item.jobs_total,
                jobs_completed=item.jobs_completed,
                jobs_failed=item.jobs_failed,
                jobs_canceled=item.jobs_canceled,
                estimated_cost_usd=item.estimated_cost_usd,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                total_tokens=item.total_tokens,
            )
            for item in summary.by_billing_mode
        ],
        by_subtenant=[
            UsageBucketOut(
                key=item.key,
                label=item.label,
                jobs_total=item.jobs_total,
                jobs_completed=item.jobs_completed,
                jobs_failed=item.jobs_failed,
                jobs_canceled=item.jobs_canceled,
                estimated_cost_usd=item.estimated_cost_usd,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                total_tokens=item.total_tokens,
            )
            for item in summary.by_subtenant
        ],
        by_provider=[
            UsageBucketOut(
                key=item.key,
                label=item.label,
                jobs_total=item.jobs_total,
                jobs_completed=item.jobs_completed,
                jobs_failed=item.jobs_failed,
                jobs_canceled=item.jobs_canceled,
                estimated_cost_usd=item.estimated_cost_usd,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                total_tokens=item.total_tokens,
            )
            for item in summary.by_provider
        ],
    )


def _provider_config_out(config: TenantProviderConfig) -> ProviderConfigOut:
    provider = get_provider_catalog_item(config.provider_slug)
    provider_name = provider.name if provider else config.provider_slug
    model_prefix = provider.model_prefix if provider else config.provider_slug
    return ProviderConfigOut(
        id=config.id,
        tenant_id=config.tenant_id,
        provider_slug=config.provider_slug,
        provider_name=provider_name,
        connection_name=config.name,
        description=config.description,
        is_default=config.is_default,
        model_prefix=model_prefix,
        billing_mode=config.billing_mode.value,
        auth_mode=config.auth_mode.value,
        has_tenant_key=has_tenant_key(config),
        platform_key_available=platform_key_available(config.provider_slug),
        api_base=config.api_base,
        api_version=config.api_version,
        extra_json=config.extra_json or {},
        is_active=config.is_active,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


def _role_assignable_by_actor(actor_role: str, requested_role: str) -> bool:
    if actor_role == "owner":
        return requested_role in {"owner", "admin", "dev", "viewer"}
    if actor_role == "admin":
        return requested_role in {"admin", "dev", "viewer"}
    return False


def _count_tenant_owners(db: Session, tenant_id: str) -> int:
    return int(
        db.scalar(
            select(func.count(User.id)).where(
                User.tenant_id == tenant_id,
                User.role == UserRole.owner,
            )
        )
        or 0
    )


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        role=user.role.value,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.get("/v1/tenants", response_model=list[TenantOut])
def list_tenants(
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> list[TenantOut]:
    return [TenantOut.model_validate(tenant) for tenant in list_accessible_tenants(db, current.principal_tenant_id)]


@router.post("/v1/tenants", response_model=TenantOut)
def create_tenant(
    payload: TenantCreate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> Tenant:
    settings = get_settings()
    is_root_create = payload.parent_tenant_id is None
    if is_root_create and current.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only owners can create top-level tenants")

    if is_root_create and settings.single_tenant_mode and db.scalar(select(func.count(Tenant.id))) > 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Single-tenant mode is enabled")

    parent: Tenant | None = None
    if payload.parent_tenant_id:
        parent = db.scalar(select(Tenant).where(Tenant.id == payload.parent_tenant_id))
        if parent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent tenant not found")
        if not is_same_or_descendant(db, current.principal_tenant_id, parent.id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        if not parent.can_create_subtenants and current.role != "owner":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Parent tenant does not allow sub-tenant creation",
            )

    existing = db.scalar(select(Tenant).where(Tenant.name == payload.name))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant already exists")

    tenant = Tenant(
        name=payload.name,
        parent_tenant_id=payload.parent_tenant_id,
        can_create_subtenants=payload.can_create_subtenants,
        inherit_provider_configs=payload.inherit_provider_configs,
        query_params_mode=payload.query_params_mode,
        query_params_json=payload.query_params_json,
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="tenant.created",
        target_type="tenant",
        target_id=tenant.id,
        diff_json={
            "name": payload.name,
            "parent_tenant_id": payload.parent_tenant_id,
            "query_params_mode": payload.query_params_mode,
            "query_params_json": payload.query_params_json,
        },
        request=request,
    )
    return tenant


@router.patch("/v1/tenants/{tenant_id}", response_model=TenantOut)
def update_tenant(
    tenant_id: str,
    payload: TenantUpdate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> Tenant:
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if not is_same_or_descendant(db, current.principal_tenant_id, tenant.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    before = {
        "name": tenant.name,
        "can_create_subtenants": tenant.can_create_subtenants,
        "inherit_provider_configs": tenant.inherit_provider_configs,
        "query_params_mode": tenant.query_params_mode.value,
        "query_params_json": tenant.query_params_json,
    }
    if payload.name is not None:
        tenant.name = payload.name
    if payload.can_create_subtenants is not None:
        tenant.can_create_subtenants = payload.can_create_subtenants
    if payload.inherit_provider_configs is not None:
        tenant.inherit_provider_configs = payload.inherit_provider_configs
    if payload.query_params_mode is not None:
        tenant.query_params_mode = payload.query_params_mode
    if payload.query_params_json is not None:
        tenant.query_params_json = payload.query_params_json
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="tenant.updated",
        target_type="tenant",
        target_id=tenant.id,
        diff_json={
            "before": before,
            "after": {
                "name": tenant.name,
                "can_create_subtenants": tenant.can_create_subtenants,
                "inherit_provider_configs": tenant.inherit_provider_configs,
                "query_params_mode": tenant.query_params_mode.value,
                "query_params_json": tenant.query_params_json,
            },
        },
        request=request,
    )
    return tenant


@router.get("/v1/users", response_model=list[UserOut])
def list_users(
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> list[UserOut]:
    users = db.scalars(select(User).where(User.tenant_id == current.tenant_id).order_by(User.created_at.asc())).all()
    return [_user_out(user) for user in users]


@router.post("/v1/users", response_model=UserOut)
def create_user(
    payload: UserCreate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> UserOut:
    email = payload.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Valid email is required")
    if len(payload.password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 8 characters")
    if not _role_assignable_by_actor(current.role, payload.role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden role assignment")

    existing = db.scalar(select(User).where(User.tenant_id == current.tenant_id, User.email == email))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")

    user = User(
        tenant_id=current.tenant_id,
        email=email,
        display_name=(payload.display_name or "").strip() or None,
        password_hash=hash_password(payload.password),
        role=UserRole(payload.role),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="user.created",
        target_type="user",
        target_id=user.id,
        diff_json={"email": user.email, "role": user.role.value},
        request=request,
    )
    return _user_out(user)


@router.patch("/v1/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    payload: UserUpdate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> UserOut:
    user = db.scalar(select(User).where(User.id == user_id, User.tenant_id == current.tenant_id))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    before = {"display_name": user.display_name, "role": user.role.value}
    if payload.role is not None:
        if not _role_assignable_by_actor(current.role, payload.role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden role assignment")
        if user.role == UserRole.owner and payload.role != "owner" and _count_tenant_owners(db, current.tenant_id) <= 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove last owner")
        user.role = UserRole(payload.role)
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip() or None

    db.add(user)
    db.commit()
    db.refresh(user)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="user.updated",
        target_type="user",
        target_id=user.id,
        diff_json={"before": before, "after": {"display_name": user.display_name, "role": user.role.value}},
        request=request,
    )
    return _user_out(user)


@router.post("/v1/users/{user_id}/password", response_model=UserOut)
def reset_user_password(
    user_id: str,
    payload: UserPasswordUpdate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> UserOut:
    if len(payload.password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 8 characters")

    user = db.scalar(select(User).where(User.id == user_id, User.tenant_id == current.tenant_id))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if current.role != "owner" and user.role == UserRole.owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only owner can reset owner password")

    user.password_hash = hash_password(payload.password)
    db.add(user)
    db.commit()
    db.refresh(user)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="user.password_reset",
        target_type="user",
        target_id=user.id,
        diff_json={},
        request=request,
    )
    return _user_out(user)


@router.delete("/v1/users/{user_id}")
def delete_user(
    user_id: str,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> dict:
    user = db.scalar(select(User).where(User.id == user_id, User.tenant_id == current.tenant_id))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == current.user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete current user")
    if current.role != "owner" and user.role == UserRole.owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only owner can delete owner users")
    if user.role == UserRole.owner and _count_tenant_owners(db, current.tenant_id) <= 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete last owner")

    db.delete(user)
    db.commit()
    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="user.deleted",
        target_type="user",
        target_id=user_id,
        diff_json={},
        request=request,
    )
    return {"ok": True}


@router.get("/v1/tenant/settings/llm", response_model=TenantLLMSettingsOut)
def get_tenant_llm_settings(
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> TenantLLMSettingsOut:
    tenant = db.scalar(select(Tenant).where(Tenant.id == current.tenant_id))
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    settings = get_settings()
    return TenantLLMSettingsOut(
        tenant_id=tenant.id,
        llm_auth_mode=tenant.llm_auth_mode.value,
        has_tenant_key=tenant_has_configured_key(tenant),
        has_platform_key=bool(settings.openai_api_key or platform_key_available("openai")),
    )


@router.post("/v1/tenant/settings/llm", response_model=TenantLLMSettingsOut)
def update_tenant_llm_settings(
    payload: TenantLLMSettingsUpdate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> TenantLLMSettingsOut:
    tenant = db.scalar(select(Tenant).where(Tenant.id == current.tenant_id))
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    previous_mode = tenant.llm_auth_mode.value
    current_ref = tenant.openai_key_ref
    settings = get_settings()

    if payload.clear_tenant_key and current_ref:
        delete_secret(current_ref)
        tenant.openai_key_ref = None

    if payload.openai_api_key:
        secret_ref = tenant.openai_key_ref or build_openai_secret_ref(tenant.id)
        try:
            put_secret(secret_ref, payload.openai_api_key)
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        tenant.openai_key_ref = secret_ref

    if payload.llm_auth_mode == "tenant":
        has_tenant_key = tenant_has_configured_key(tenant)
        if not has_tenant_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tenant mode requires a tenant OpenAI API key",
            )
    if payload.llm_auth_mode == "platform" and not (settings.openai_api_key or platform_key_available("openai")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Platform mode requires OPENAI_API_KEY in environment",
        )

    tenant.llm_auth_mode = LlmAuthMode(payload.llm_auth_mode)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="tenant.llm_settings.updated",
        target_type="tenant",
        target_id=tenant.id,
        diff_json={
            "before_mode": previous_mode,
            "after_mode": tenant.llm_auth_mode.value,
            "key_updated": bool(payload.openai_api_key),
            "key_cleared": payload.clear_tenant_key,
        },
        request=request,
    )

    return TenantLLMSettingsOut(
        tenant_id=tenant.id,
        llm_auth_mode=tenant.llm_auth_mode.value,
        has_tenant_key=tenant_has_configured_key(tenant),
        has_platform_key=bool(settings.openai_api_key or platform_key_available("openai")),
    )


@router.get("/v1/providers/catalog", response_model=list[ProviderCatalogOut])
def list_provider_catalog_api(
    _: SessionUser = Depends(get_session_user),
) -> list[ProviderCatalogOut]:
    return [
        ProviderCatalogOut(
            slug=item.slug,
            name=item.name,
            logo_path=item.logo_path,
            model_prefix=item.model_prefix,
            default_model=item.default_model,
            recommended_models=list(item.recommended_models),
            platform_key_env=item.platform_key_env,
            requires_api_key=item.requires_api_key,
            docs_url=item.docs_url,
            realtime_docs_url=item.realtime_docs_url,
        )
        for item in list_provider_catalog()
    ]


@router.get("/v1/providers", response_model=list[ProviderConfigOut])
def list_provider_configs_api(
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> list[ProviderConfigOut]:
    configs = list_tenant_provider_configs(db, current.tenant_id)
    return [_provider_config_out(config) for config in configs]


@router.post("/v1/providers", response_model=ProviderConfigOut)
def create_or_update_provider_config_api(
    payload: ProviderConfigCreate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> ProviderConfigOut:
    normalized = ensure_supported_provider_slug(payload.provider_slug)
    effective_base, effective_version = resolve_provider_endpoint_options(
        normalized,
        api_base=payload.api_base,
        api_version=payload.api_version,
        use_platform_defaults=False,
    )
    provider_spec = get_provider_spec(normalized)
    if provider_spec is not None:
        extra_values = payload.extra_json or {}
        for field in provider_spec.connection_fields:
            value = ""
            if field.key == "api_key":
                value = (payload.api_key or "").strip()
            elif field.key == "api_base":
                value = effective_base or ""
            elif field.key == "api_version":
                value = effective_version or ""
            else:
                value = str(extra_values.get(field.key, "")).strip()
            if field.required and not value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{field.label} is required",
                )

    if payload.api_key:
        validation = validate_provider_api_key(
            provider_slug=normalized,
            api_key=payload.api_key,
            api_base=effective_base,
            api_version=effective_version,
        )
        if not validation.valid and validation.definitive:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=validation.message)

    config = upsert_tenant_provider_config(
        db,
        tenant_id=current.tenant_id,
        provider_slug=normalized,
        provider_config_id=payload.provider_config_id,
        connection_name=payload.connection_name,
        description=payload.description,
        is_default=payload.is_default,
        billing_mode=payload.billing_mode,
        auth_mode=payload.auth_mode,
        api_key=payload.api_key,
        clear_api_key=payload.clear_api_key,
        api_base=effective_base,
        api_version=effective_version,
        extra_json=payload.extra_json,
        is_active=payload.is_active,
    )
    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="provider.config.upserted",
        target_type="provider_config",
        target_id=config.id,
        diff_json={
            "provider_slug": normalized,
            "connection_name": config.name,
            "billing_mode": config.billing_mode.value,
            "auth_mode": config.auth_mode.value,
        },
        request=request,
    )
    return _provider_config_out(config)


@router.patch("/v1/providers/{provider_slug}", response_model=ProviderConfigOut)
def patch_provider_config_api(
    provider_slug: str,
    payload: ProviderConfigUpdate,
    request: Request,
    provider_config_id: str | None = None,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> ProviderConfigOut:
    normalized_slug = ensure_supported_provider_slug(provider_slug)
    existing = (
        get_tenant_provider_config_by_id(db, current.tenant_id, provider_config_id)
        if provider_config_id
        else get_tenant_provider_config(
            db,
            current.tenant_id,
            normalized_slug,
        )
    )
    if existing is not None and existing.provider_slug != normalized_slug:
        existing = None
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider config not found")

    api_base = payload.api_base if payload.api_base is not None else existing.api_base
    api_version = payload.api_version if payload.api_version is not None else existing.api_version
    effective_base, effective_version = resolve_provider_endpoint_options(
        existing.provider_slug,
        api_base=api_base,
        api_version=api_version,
        use_platform_defaults=False,
    )
    provider_spec = get_provider_spec(existing.provider_slug)
    if provider_spec is not None:
        merged_extra = dict(existing.extra_json or {})
        if payload.extra_json is not None:
            merged_extra.update(payload.extra_json)
        existing_tenant_key = has_tenant_key(existing)
        incoming_key = (payload.api_key or "").strip()
        for field in provider_spec.connection_fields:
            value = ""
            if field.key == "api_key":
                value = incoming_key
                if not value and existing_tenant_key and not payload.clear_api_key:
                    value = "__existing_key__"
            elif field.key == "api_base":
                value = effective_base or ""
            elif field.key == "api_version":
                value = effective_version or ""
            else:
                value = str(merged_extra.get(field.key, "")).strip()
            if field.required and not value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{field.label} is required",
                )

    if payload.api_key:
        validation = validate_provider_api_key(
            provider_slug=existing.provider_slug,
            api_key=payload.api_key,
            api_base=effective_base,
            api_version=effective_version,
        )
        if not validation.valid and validation.definitive:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=validation.message)

    config = upsert_tenant_provider_config(
        db,
        tenant_id=current.tenant_id,
        provider_slug=existing.provider_slug,
        provider_config_id=existing.id,
        connection_name=payload.connection_name if payload.connection_name is not None else existing.name,
        description=payload.description if payload.description is not None else existing.description,
        is_default=payload.is_default if payload.is_default is not None else existing.is_default,
        billing_mode=payload.billing_mode,
        auth_mode=payload.auth_mode,
        api_key=payload.api_key,
        clear_api_key=payload.clear_api_key,
        api_base=effective_base,
        api_version=effective_version,
        extra_json=payload.extra_json if payload.extra_json is not None else existing.extra_json,
        is_active=payload.is_active if payload.is_active is not None else existing.is_active,
    )

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="provider.config.updated",
        target_type="provider_config",
        target_id=config.id,
        diff_json={
            "provider_slug": config.provider_slug,
            "connection_name": config.name,
            "billing_mode": config.billing_mode.value,
            "auth_mode": config.auth_mode.value,
        },
        request=request,
    )
    return _provider_config_out(config)


@router.delete("/v1/providers/{provider_slug}")
def delete_provider_config_api(
    provider_slug: str,
    request: Request,
    provider_config_id: str | None = None,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> dict:
    normalized = ensure_supported_provider_slug(provider_slug)
    removed = delete_tenant_provider_config(
        db,
        tenant_id=current.tenant_id,
        provider_slug=normalized,
        provider_config_id=provider_config_id,
    )
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider config not found")

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="provider.config.deleted",
        target_type="provider_config",
        target_id=provider_config_id or normalized,
        request=request,
    )
    return {"ok": True}


@router.get("/v1/targets", response_model=list[TargetOut])
def list_targets_api(
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> list[Target]:
    return list_targets(db, current.tenant_id)


@router.post("/v1/targets", response_model=TargetOut)
def create_target_api(
    payload: TargetCreate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> Target:
    existing = db.scalar(select(Target).where(Target.tenant_id == current.tenant_id, Target.name == payload.name))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Target name already exists")

    try:
        target = create_target_record(db, current.tenant_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="target.created",
        target_type="target",
        target_id=target.id,
        diff_json={
            "name": target.name,
            "provider_config_id": target.provider_config_id,
            "provider_slug": target.provider_slug,
            "capability_profile": target.capability_profile,
            "model_identifier": target.model_identifier,
        },
        request=request,
    )
    return target


@router.patch("/v1/targets/{target_id}", response_model=TargetOut)
def update_target_api(
    target_id: str,
    payload: TargetUpdate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> Target:
    target = get_target(db, current.tenant_id, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")

    if payload.name is not None and payload.name != target.name:
        name_taken = db.scalar(select(Target.id).where(Target.tenant_id == current.tenant_id, Target.name == payload.name))
        if name_taken:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Target name already exists")

    try:
        updated = update_target_record(db, target, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="target.updated",
        target_type="target",
        target_id=updated.id,
        diff_json={
            "name": updated.name,
            "provider_config_id": updated.provider_config_id,
            "provider_slug": updated.provider_slug,
            "capability_profile": updated.capability_profile,
            "model_identifier": updated.model_identifier,
            "is_active": updated.is_active,
        },
        request=request,
    )
    return updated


@router.post("/v1/targets/{target_id}/verify", response_model=TargetVerifyResponse)
def verify_target_api(
    target_id: str,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> TargetVerifyResponse:
    target = get_target(db, current.tenant_id, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    ok, message = verify_target(db, target)
    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="target.verified",
        target_type="target",
        target_id=target.id,
        diff_json={"ok": ok, "message": message},
        request=request,
    )
    return TargetVerifyResponse(ok=ok, message=message, target=TargetOut.model_validate(target))


@router.delete("/v1/targets/{target_id}")
def delete_target_api(
    target_id: str,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> dict:
    target = get_target(db, current.tenant_id, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")

    in_use = db.scalar(select(func.count(EndpointVersion.id)).where(EndpointVersion.target_id == target.id)) or 0
    if in_use > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target is in use by endpoint versions and cannot be deleted",
        )

    delete_target_record(db, target)
    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="target.deleted",
        target_type="target",
        target_id=target_id,
        request=request,
    )
    return {"ok": True}


@router.get("/v1/portal-links", response_model=list[PortalLinkOut])
def list_portal_links_api(
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> list[PortalLink]:
    return list_portal_links(db, current.tenant_id)


@router.post("/v1/portal-links", response_model=PortalLinkCreateOut)
def create_portal_link_api(
    payload: PortalLinkCreate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> PortalLinkCreateOut:
    if payload.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="expires_at must be in the future")

    link, raw_token = create_portal_link(
        db,
        tenant_id=current.tenant_id,
        created_by_user_id=current.user_id,
        payload=payload,
    )
    access_url = f"/portal/access/{raw_token}"
    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="portal_link.created",
        target_type="portal_link",
        target_id=link.id,
        diff_json={
            "subtenant_code": link.subtenant_code,
            "permissions": link.permissions_json,
            "expires_at": link.expires_at.isoformat(),
        },
        request=request,
    )
    return PortalLinkCreateOut(link=PortalLinkOut.model_validate(link), access_url=access_url)


@router.post("/v1/portal-links/{link_id}/revoke", response_model=PortalLinkOut)
def revoke_portal_link_api(
    link_id: str,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> PortalLinkOut:
    link = get_portal_link(db, current.tenant_id, link_id)
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portal link not found")
    revoked = revoke_portal_link(db, link)
    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="portal_link.revoked",
        target_type="portal_link",
        target_id=revoked.id,
        request=request,
    )
    return PortalLinkOut.model_validate(revoked)


@router.get("/v1/usage/summary", response_model=UsageSummaryOut)
def usage_summary_api(
    window_hours: int = 24,
    bucket_limit: int = 12,
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> UsageSummaryOut:
    summary = build_usage_summary(
        db,
        tenant_id=current.tenant_id,
        window_hours=window_hours,
        bucket_limit=bucket_limit,
    )
    return _usage_summary_out(summary)


@router.get("/v1/pricing-rates", response_model=list[BuiltinPricingRateOut])
def list_pricing_rates_api(
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> list[BuiltinPricingRateOut]:
    return [
        BuiltinPricingRateOut(
            provider_slug=str(row["provider_slug"]),
            model_pattern=str(row["model_pattern"]),
            input_per_1m_usd=float(row["input_per_1m_usd"]),
            output_per_1m_usd=float(row["output_per_1m_usd"]),
            cached_input_per_1m_usd=(
                float(row["cached_input_per_1m_usd"]) if row.get("cached_input_per_1m_usd") is not None else None
            ),
            source="builtin_estimate",
        )
        for row in list_builtin_pricing_rates()
    ]


@router.delete("/v1/tenants/{tenant_id}")
def delete_tenant(
    tenant_id: str,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> dict:
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if tenant.id == current.principal_tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete principal tenant")
    if not is_same_or_descendant(db, current.principal_tenant_id, tenant.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="tenant.deleted",
        target_type="tenant",
        target_id=tenant_id,
        request=request,
    )

    if tenant.openai_key_ref:
        delete_secret(tenant.openai_key_ref)

    db.delete(tenant)
    db.commit()
    return {"ok": True}


@router.get("/v1/endpoints", response_model=list[EndpointOut])
def list_endpoints(
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> list[Endpoint]:
    return db.scalars(
        select(Endpoint)
        .where(Endpoint.tenant_id == current.tenant_id)
        .order_by(Endpoint.created_at.desc())
    ).all()


@router.post("/v1/endpoints", response_model=EndpointOut)
def create_endpoint(
    payload: EndpointCreate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> Endpoint:
    endpoint = Endpoint(tenant_id=current.tenant_id, name=payload.name, description=payload.description)
    db.add(endpoint)
    db.commit()
    db.refresh(endpoint)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="endpoint.created",
        target_type="endpoint",
        target_id=endpoint.id,
        diff_json={"name": payload.name},
        request=request,
    )
    return endpoint


@router.get("/v1/endpoints/{endpoint_id}", response_model=EndpointOut)
def get_endpoint(
    endpoint_id: str,
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> Endpoint:
    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == current.tenant_id))
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
    return endpoint


@router.patch("/v1/endpoints/{endpoint_id}", response_model=EndpointOut)
def update_endpoint(
    endpoint_id: str,
    payload: EndpointUpdate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> Endpoint:
    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == current.tenant_id))
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    before = {"name": endpoint.name, "description": endpoint.description}

    if payload.name is not None:
        endpoint.name = payload.name
    if payload.description is not None:
        endpoint.description = payload.description

    db.add(endpoint)
    db.commit()
    db.refresh(endpoint)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="endpoint.updated",
        target_type="endpoint",
        target_id=endpoint.id,
        diff_json={"before": before, "after": {"name": endpoint.name, "description": endpoint.description}},
        request=request,
    )

    return endpoint


@router.delete("/v1/endpoints/{endpoint_id}")
def delete_endpoint(
    endpoint_id: str,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> dict:
    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == current.tenant_id))
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="endpoint.deleted",
        target_type="endpoint",
        target_id=endpoint.id,
        request=request,
    )

    db.delete(endpoint)
    db.commit()
    return {"ok": True}


@router.get("/v1/endpoints/{endpoint_id}/versions", response_model=list[EndpointVersionOut])
def list_endpoint_versions(
    endpoint_id: str,
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> list[EndpointVersion]:
    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == current.tenant_id))
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    return db.scalars(
        select(EndpointVersion)
        .where(EndpointVersion.endpoint_id == endpoint_id)
        .order_by(EndpointVersion.version.desc())
    ).all()


@router.post("/v1/endpoints/{endpoint_id}/versions", response_model=EndpointVersionOut)
def create_endpoint_version(
    endpoint_id: str,
    payload: EndpointVersionCreate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> EndpointVersion:
    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == current.tenant_id))
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    try:
        version = create_endpoint_version_record(
            db,
            endpoint=endpoint,
            tenant_id=current.tenant_id,
            created_by_user_id=current.user_id,
            payload=payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if endpoint.active_version_id is None:
        endpoint.active_version_id = version.id
        db.add(endpoint)
        db.commit()

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="endpoint.version.created",
        target_type="endpoint_version",
        target_id=version.id,
        diff_json={"endpoint_id": endpoint_id, "version": version.version},
        request=request,
    )
    return version


@router.post("/v1/endpoints/{endpoint_id}/activate", response_model=EndpointOut)
def activate_endpoint_version(
    endpoint_id: str,
    payload: ActivateVersionRequest,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> Endpoint:
    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == current.tenant_id))
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    version = db.scalar(
        select(EndpointVersion).where(EndpointVersion.id == payload.version_id, EndpointVersion.endpoint_id == endpoint_id)
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")

    endpoint.active_version_id = version.id
    db.add(endpoint)
    db.commit()
    db.refresh(endpoint)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="endpoint.version.activated",
        target_type="endpoint",
        target_id=endpoint.id,
        diff_json={"active_version_id": version.id},
        request=request,
    )

    return endpoint


@router.post("/v1/endpoints/{endpoint_id}/prompt", response_model=EndpointVersionOut)
def update_prompt_by_endpoint(
    endpoint_id: str,
    payload: PromptUpdateRequest,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> EndpointVersion:
    version = create_endpoint_version(
        endpoint_id,
        EndpointVersionCreate(
            system_prompt=payload.system_prompt,
            input_template=payload.input_template,
            variable_schema_json=payload.variable_schema_json,
            target_id=payload.target_id,
            provider=payload.provider,
            model=payload.model,
            params_json=payload.params_json,
            persona_id=payload.persona_id,
            context_block_ids=payload.context_block_ids,
        ),
        request,
        current,
        db,
    )
    activate_endpoint_version(
        endpoint_id,
        ActivateVersionRequest(version_id=version.id),
        request,
        current,
        db,
    )
    return version


@router.get("/v1/personas", response_model=list[PersonaOut])
def list_personas_api(
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> list[Persona]:
    return db.scalars(
        select(Persona).where(Persona.tenant_id == current.tenant_id).order_by(Persona.name.asc())
    ).all()


@router.post("/v1/personas", response_model=PersonaOut)
def create_persona_api(
    payload: PersonaCreate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> Persona:
    existing = db.scalar(select(Persona).where(Persona.tenant_id == current.tenant_id, Persona.name == payload.name))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Persona name already exists")

    persona = Persona(
        tenant_id=current.tenant_id,
        name=payload.name,
        description=payload.description,
        instructions=payload.instructions,
        style_json=payload.style_json,
    )
    db.add(persona)
    db.commit()
    db.refresh(persona)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="persona.created",
        target_type="persona",
        target_id=persona.id,
        diff_json={"name": persona.name},
        request=request,
    )
    return persona


@router.patch("/v1/personas/{persona_id}", response_model=PersonaOut)
def update_persona_api(
    persona_id: str,
    payload: PersonaUpdate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> Persona:
    persona = db.scalar(select(Persona).where(Persona.id == persona_id, Persona.tenant_id == current.tenant_id))
    if persona is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Persona not found")

    before = {
        "name": persona.name,
        "description": persona.description,
        "instructions": persona.instructions,
        "style_json": persona.style_json,
    }
    if payload.name is not None:
        persona.name = payload.name
    if payload.description is not None:
        persona.description = payload.description
    if payload.instructions is not None:
        persona.instructions = payload.instructions
    if payload.style_json is not None:
        persona.style_json = payload.style_json

    db.add(persona)
    db.commit()
    db.refresh(persona)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="persona.updated",
        target_type="persona",
        target_id=persona.id,
        diff_json={"before": before, "after": {"name": persona.name}},
        request=request,
    )
    return persona


@router.delete("/v1/personas/{persona_id}")
def delete_persona_api(
    persona_id: str,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> dict:
    persona = db.scalar(select(Persona).where(Persona.id == persona_id, Persona.tenant_id == current.tenant_id))
    if persona is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Persona not found")

    count = db.scalar(select(func.count(EndpointVersion.id)).where(EndpointVersion.persona_id == persona.id)) or 0
    if count > 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Persona is used by endpoint versions")

    db.delete(persona)
    db.commit()
    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="persona.deleted",
        target_type="persona",
        target_id=persona_id,
        request=request,
    )
    return {"ok": True}


@router.get("/v1/context-blocks", response_model=list[ContextBlockOut])
def list_context_blocks_api(
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> list[ContextBlock]:
    return db.scalars(
        select(ContextBlock).where(ContextBlock.tenant_id == current.tenant_id).order_by(ContextBlock.name.asc())
    ).all()


@router.post("/v1/context-blocks", response_model=ContextBlockOut)
def create_context_block_api(
    payload: ContextBlockCreate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> ContextBlock:
    existing = db.scalar(
        select(ContextBlock).where(ContextBlock.tenant_id == current.tenant_id, ContextBlock.name == payload.name)
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Context block name already exists")

    block = ContextBlock(
        tenant_id=current.tenant_id,
        name=payload.name,
        content=payload.content,
        tags=payload.tags,
    )
    db.add(block)
    db.commit()
    db.refresh(block)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="context_block.created",
        target_type="context_block",
        target_id=block.id,
        diff_json={"name": block.name},
        request=request,
    )
    return block


@router.patch("/v1/context-blocks/{block_id}", response_model=ContextBlockOut)
def update_context_block_api(
    block_id: str,
    payload: ContextBlockUpdate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> ContextBlock:
    block = db.scalar(select(ContextBlock).where(ContextBlock.id == block_id, ContextBlock.tenant_id == current.tenant_id))
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Context block not found")

    if payload.name is not None:
        block.name = payload.name
    if payload.content is not None:
        block.content = payload.content
    if payload.tags is not None:
        block.tags = payload.tags

    db.add(block)
    db.commit()
    db.refresh(block)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="context_block.updated",
        target_type="context_block",
        target_id=block.id,
        request=request,
    )
    return block


@router.delete("/v1/context-blocks/{block_id}")
def delete_context_block_api(
    block_id: str,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> dict:
    block = db.scalar(select(ContextBlock).where(ContextBlock.id == block_id, ContextBlock.tenant_id == current.tenant_id))
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Context block not found")

    db.execute(
        EndpointVersionContext.__table__.delete().where(EndpointVersionContext.context_block_id == block.id)
    )
    db.delete(block)
    db.commit()
    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="context_block.deleted",
        target_type="context_block",
        target_id=block_id,
        request=request,
    )
    return {"ok": True}


@router.get("/v1/tenant/variables", response_model=list[TenantVariableOut])
def list_tenant_variables_api(
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> list[TenantVariable]:
    return db.scalars(
        select(TenantVariable).where(TenantVariable.tenant_id == current.tenant_id).order_by(TenantVariable.key.asc())
    ).all()


@router.post("/v1/tenant/variables", response_model=TenantVariableOut)
def create_tenant_variable_api(
    payload: TenantVariableCreate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> TenantVariable:
    existing = db.scalar(
        select(TenantVariable).where(TenantVariable.tenant_id == current.tenant_id, TenantVariable.key == payload.key)
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Variable key already exists")

    variable = TenantVariable(
        tenant_id=current.tenant_id,
        key=payload.key,
        value=payload.value,
        is_secret=payload.is_secret,
    )
    db.add(variable)
    db.commit()
    db.refresh(variable)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="tenant_variable.created",
        target_type="tenant_variable",
        target_id=variable.id,
        diff_json={"key": variable.key, "is_secret": variable.is_secret},
        request=request,
    )
    return variable


@router.patch("/v1/tenant/variables/{variable_id}", response_model=TenantVariableOut)
def update_tenant_variable_api(
    variable_id: str,
    payload: TenantVariableUpdate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> TenantVariable:
    variable = db.scalar(
        select(TenantVariable).where(TenantVariable.id == variable_id, TenantVariable.tenant_id == current.tenant_id)
    )
    if variable is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variable not found")

    if payload.value is not None:
        variable.value = payload.value
    if payload.is_secret is not None:
        variable.is_secret = payload.is_secret
    db.add(variable)
    db.commit()
    db.refresh(variable)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="tenant_variable.updated",
        target_type="tenant_variable",
        target_id=variable.id,
        diff_json={"key": variable.key},
        request=request,
    )
    return variable


@router.delete("/v1/tenant/variables/{variable_id}")
def delete_tenant_variable_api(
    variable_id: str,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> dict:
    variable = db.scalar(
        select(TenantVariable).where(TenantVariable.id == variable_id, TenantVariable.tenant_id == current.tenant_id)
    )
    if variable is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variable not found")
    db.delete(variable)
    db.commit()

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="tenant_variable.deleted",
        target_type="tenant_variable",
        target_id=variable_id,
        request=request,
    )
    return {"ok": True}


@router.get("/v1/api-keys", response_model=list[ApiKeyOut])
def list_api_keys(
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> list[ApiKey]:
    return db.scalars(
        select(ApiKey).where(ApiKey.tenant_id == current.tenant_id).order_by(ApiKey.created_at.desc())
    ).all()


@router.post("/v1/api-keys", response_model=ApiKeyCreateOut)
def create_api_key(
    payload: ApiKeyCreate,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin", "dev")),
    db: Session = Depends(get_db),
) -> ApiKeyCreateOut:
    key, raw = create_virtual_key(db, current.tenant_id, payload)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="api_key.created",
        target_type="api_key",
        target_id=key.id,
        diff_json={"name": key.name, "scopes": key.scopes},
        request=request,
    )

    return ApiKeyCreateOut(id=key.id, name=key.name, key_prefix=key.key_prefix, key=raw)


@router.post("/v1/api-keys/{key_id}/deactivate", response_model=ApiKeyOut)
def deactivate_api_key(
    key_id: str,
    request: Request,
    current: SessionUser = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
) -> ApiKey:
    key = db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == current.tenant_id))
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    key.is_active = False
    db.add(key)
    db.commit()
    db.refresh(key)

    log_action(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.user_id,
        action="api_key.deactivated",
        target_type="api_key",
        target_id=key.id,
        request=request,
    )
    return key


@router.get("/v1/admin/jobs", response_model=list[JobOut])
def list_jobs(
    endpoint_id: str | None = None,
    status_filter: JobStatus | None = None,
    limit: int = 100,
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> list[Job]:
    filters = [Job.tenant_id == current.tenant_id]
    if endpoint_id:
        filters.append(Job.endpoint_id == endpoint_id)
    if status_filter:
        filters.append(Job.status == status_filter)

    return db.scalars(
        select(Job)
        .where(and_(*filters))
        .order_by(Job.created_at.desc())
        .limit(min(max(limit, 1), 500))
    ).all()


@router.get("/v1/admin/jobs/{job_id}", response_model=JobOut)
def get_job_detail(
    job_id: str,
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id, Job.tenant_id == current.tenant_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.get("/v1/admin/training", response_model=list[TrainingEventOut])
def list_training_events(
    endpoint_id: str | None = None,
    feedback: str | None = None,
    limit: int = 100,
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> list[TrainingEvent]:
    filters = [TrainingEvent.tenant_id == current.tenant_id]
    if endpoint_id:
        filters.append(TrainingEvent.endpoint_id == endpoint_id)
    if feedback:
        filters.append(TrainingEvent.feedback == feedback)

    return db.scalars(
        select(TrainingEvent)
        .where(and_(*filters))
        .order_by(TrainingEvent.created_at.desc())
        .limit(min(max(limit, 1), 500))
    ).all()


@router.post("/v1/training/export")
def export_training(
    payload: TrainingExportRequest,
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    events = query_training_events(db, current.tenant_id, payload)
    filename = f"training_export_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.jsonl"
    return StreamingResponse(
        export_training_jsonl(events),
        media_type="application/jsonl",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/v1/admin/dashboard")
def dashboard_metrics(
    current: SessionUser = Depends(get_session_user),
    db: Session = Depends(get_db),
) -> dict:
    since = datetime.now(UTC) - timedelta(hours=24)
    queued = db.scalar(
        select(func.count(Job.id)).where(Job.tenant_id == current.tenant_id, Job.status == JobStatus.queued)
    )
    usage_count = db.scalar(select(func.count(Job.id)).where(Job.tenant_id == current.tenant_id, Job.created_at >= since))
    completed_today = db.scalar(
        select(func.count(Job.id)).where(
            Job.tenant_id == current.tenant_id,
            Job.status == JobStatus.completed,
            Job.created_at >= since,
        )
    )
    spend_last_24h = db.scalar(
        select(func.coalesce(func.sum(Job.estimated_cost_usd), 0.0)).where(
            Job.tenant_id == current.tenant_id,
            Job.created_at >= since,
        )
    )
    spend_all_time = db.scalar(
        select(func.coalesce(func.sum(Job.estimated_cost_usd), 0.0)).where(Job.tenant_id == current.tenant_id)
    )

    return {
        "queued_jobs": queued or 0,
        "jobs_last_24h": usage_count or 0,
        "completed_last_24h": completed_today or 0,
        "spend_last_24h": float(spend_last_24h or 0.0),
        "spend_all_time": float(spend_all_time or 0.0),
    }
