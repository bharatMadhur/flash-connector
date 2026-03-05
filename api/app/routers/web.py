"""Server-rendered admin console routes and page handlers."""

import logging
import json
import secrets
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.provider_catalog import (
    ensure_supported_provider_slug,
    get_model_parameters,
    get_provider_catalog_item,
    list_equivalent_models,
    list_provider_catalog,
    list_provider_models,
    normalize_provider_slug,
)
from app.core.provider_profiles import is_azure_provider_slug
from app.core.provider_registry import get_provider_spec
from app.dependencies import SessionUser, csrf_protect, ensure_csrf_token, get_optional_session_user
from app.models import (
    ApiKey,
    AuditLog,
    ContextBlock,
    Endpoint,
    EndpointVersion,
    EndpointVersionContext,
    Job,
    JobStatus,
    LlmAuthMode,
    Persona,
    PortalLink,
    ProviderBatchRun,
    SaveMode,
    Target,
    Tenant,
    TenantVariable,
    TrainingEvent,
    User,
    UserRole,
)
from app.schemas.api_keys import ApiKeyCreate
from app.schemas.endpoints import EndpointVersionCreate
from app.schemas.jobs import JobCreateRequest
from app.schemas.portal import PortalLinkCreate
from app.schemas.provider_batches import ProviderBatchCreateRequest, ProviderBatchItemRequest
from app.schemas.targets import TargetCreate, TargetUpdate
from app.schemas.training import SaveTrainingRequest, TrainingExportRequest
from app.services.api_keys import create_virtual_key
from app.services.oidc import (
    OidcAuthError,
    build_authorization_url,
    build_code_challenge,
    build_logout_url,
    exchange_code_for_tokens,
    generate_code_verifier,
    parse_and_validate_id_token,
    provision_user_from_claims,
    sanitize_next_path,
)
from app.services.jobs import create_job, get_active_version, get_job_for_tenant
from app.services.llm import run_provider_completion
from app.services.model_params import ModelParamValidationError, validate_model_params
from app.services.prompt_studio import (
    collect_request_text,
    compose_system_prompt,
    list_context_blocks,
    list_context_blocks_for_version,
    list_personas,
    list_tenant_variables,
    render_job_input,
    tenant_variables_map,
)
from app.services.provider_validation import validate_provider_api_key
from app.services.provider_batches import (
    create_provider_batch_run,
    get_provider_batch_for_tenant,
    list_jobs_for_provider_batch,
    list_provider_batches_for_tenant,
    request_cancel_provider_batch_run,
)
from app.services.pricing import list_builtin_pricing_rates
from app.services.providers import (
    delete_tenant_provider_config,
    get_effective_provider_config,
    get_tenant_provider_config_by_id,
    has_tenant_key,
    list_ready_provider_catalog_for_tenant,
    list_ready_provider_connections_for_tenant,
    list_tenant_provider_configs,
    platform_key_available,
    provider_config_is_ready,
    provider_catalog_for_tenant,
    resolve_provider_endpoint_options,
    resolve_provider_credentials,
    upsert_tenant_provider_config,
)
from app.services.token_advisor import build_token_cost_advisor
from app.services.portal import (
    PERMISSION_ADD_FEEDBACK,
    PERMISSION_EDIT_IDEAL_OUTPUT,
    PERMISSION_EXPORT_TRAINING,
    PERMISSION_VIEW_JOBS,
    create_portal_link,
    get_portal_link,
    link_permissions,
    list_portal_links,
    resolve_portal_token,
    revoke_portal_link,
)
from app.services.queue import get_queue
from app.services.tenant_llm import build_openai_secret_ref, tenant_has_configured_key
from app.services.tenant_secrets import delete_secret, put_secret
from app.services.training import create_training_event_from_job, export_training_jsonl, query_training_events
from app.services.tenants import (
    build_tenant_breadcrumb,
    is_same_or_descendant,
    list_accessible_tenants,
    resolve_effective_query_params,
)
from app.services.targets import create_target_record, delete_target_record, get_target, list_targets, update_target_record, verify_target
from app.services.usage_costs import build_usage_summary
from app.services.versioning import create_endpoint_version_record
from app.core.security import hash_password, verify_password

router = APIRouter(tags=["web"], dependencies=[Depends(csrf_protect)])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
logger = logging.getLogger(__name__)


def _ensure_user(session_user: SessionUser | None):
    if session_user is None:
        return RedirectResponse("/login", status_code=303)
    return None


def _portal_session_payload(request: Request, db: Session | None = None) -> dict[str, Any] | None:
    link_id = request.session.get("portal_link_id")
    tenant_id = request.session.get("portal_tenant_id")
    subtenant_code = request.session.get("portal_subtenant_code")
    expires_at_ts = request.session.get("portal_expires_at_ts")
    permissions = request.session.get("portal_permissions")

    if not (link_id and tenant_id and subtenant_code and expires_at_ts and isinstance(permissions, list)):
        return None

    try:
        expires_at_ts_value = int(expires_at_ts)
    except (TypeError, ValueError):
        return None
    now_ts = int(datetime.now(UTC).timestamp())
    if expires_at_ts_value <= now_ts:
        _clear_portal_session(request)
        return None

    payload = {
        "link_id": link_id,
        "tenant_id": tenant_id,
        "subtenant_code": subtenant_code,
        "expires_at_ts": expires_at_ts_value,
        "permissions": [str(item) for item in permissions],
    }
    if db is not None:
        link = db.scalar(
            select(PortalLink).where(
                PortalLink.id == str(link_id),
                PortalLink.tenant_id == str(tenant_id),
            )
        )
        if link is None or link.is_revoked:
            _clear_portal_session(request)
            return None
        if link.subtenant_code != str(subtenant_code):
            _clear_portal_session(request)
            return None
        if link.expires_at <= datetime.now(UTC):
            _clear_portal_session(request)
            return None
    return payload


def _clear_portal_session(request: Request) -> None:
    for key in (
        "portal_link_id",
        "portal_tenant_id",
        "portal_subtenant_code",
        "portal_permissions",
        "portal_expires_at_ts",
    ):
        request.session.pop(key, None)


def _ensure_portal_permission(request: Request, db: Session, permission: str) -> dict[str, Any] | RedirectResponse:
    payload = _portal_session_payload(request, db)
    if payload is None:
        return RedirectResponse("/portal/session-expired", status_code=303)
    if permission not in set(payload["permissions"]):
        return RedirectResponse("/portal/review/jobs", status_code=303)
    return payload


def _tenant_name(db: Session, tenant_id: str | None) -> str | None:
    if not tenant_id:
        return None
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id))
    return tenant.name if tenant else None


def _ensure_local_auth_principal(db: Session) -> tuple[User, Tenant]:
    settings = get_settings()
    tenant = db.scalar(select(Tenant).where(Tenant.name == settings.default_tenant_name))
    if tenant is None:
        tenant = Tenant(
            name=settings.default_tenant_name,
            can_create_subtenants=True,
            inherit_provider_configs=True,
        )
        db.add(tenant)
        db.flush()

    email = (settings.local_auth_email or "test@local.dev").strip().lower()
    role = UserRole(settings.local_auth_role)
    user = db.scalar(select(User).where(User.tenant_id == tenant.id, User.email == email))
    if user is None:
        user = User(
            tenant_id=tenant.id,
            email=email,
            password_hash=hash_password(settings.local_auth_password),
            role=role,
            display_name="Local Test User",
        )
    else:
        user.role = role
        user.display_name = user.display_name or "Local Test User"
    db.add(user)
    db.commit()
    db.refresh(user)
    db.refresh(tenant)
    return user, tenant


def _assign_session_user(request: Request, user: User, tenant: Tenant) -> None:
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["tenant_id"] = tenant.id
    request.session["principal_tenant_id"] = tenant.id
    request.session["active_tenant_id"] = tenant.id
    request.session["role"] = user.role.value
    request.session["email"] = user.email
    request.session["display_name"] = user.display_name
    request.session["id_token"] = None


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


def _base_context(
    *,
    request: Request,
    db: Session,
    session_user: SessionUser | None,
    active_nav: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build shared template context values used by all console pages."""
    settings = get_settings()
    tenant_name = _tenant_name(db, session_user.tenant_id if session_user else None)
    accessible_tenants: list[Tenant] = []
    tenant_breadcrumb: list[Tenant] = []
    ready_provider_catalog = []
    if session_user is not None:
        accessible_tenants = list_accessible_tenants(db, session_user.principal_tenant_id)
        tenant_breadcrumb = build_tenant_breadcrumb(db, session_user.tenant_id)
        ready_provider_catalog = list_ready_provider_catalog_for_tenant(db, session_user.tenant_id)

    context: dict[str, Any] = {
        "request": request,
        "active_nav": active_nav,
        "session_user": session_user,
        "csrf_token": ensure_csrf_token(request),
        "tenant_name": tenant_name,
        "active_tenant_id": session_user.tenant_id if session_user else None,
        "principal_tenant_id": session_user.principal_tenant_id if session_user else None,
        "accessible_tenants": accessible_tenants,
        "tenant_breadcrumb": tenant_breadcrumb,
        "nav_ready_provider_slugs": [provider.slug for provider in ready_provider_catalog],
        "nav_ready_provider_count": len(ready_provider_catalog),
        "nav_has_ready_providers": len(ready_provider_catalog) > 0,
        "runtime_mode": getattr(settings, "runtime_mode", "sandbox"),
        "runtime_environment": getattr(settings, "environment", "development"),
    }
    context.update(kwargs)
    return context


def _parse_json(raw: str, default: Any) -> Any:
    value = (raw or "").strip()
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _parse_nonnegative_float(raw: str, default: float | None = None) -> float | None:
    value = (raw or "").strip()
    if not value:
        return default
    try:
        return max(float(value), 0.0)
    except ValueError:
        return default


def _parse_bounded_float(raw: str, default: float, *, min_value: float, max_value: float) -> float:
    value = (raw or "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return min(max(parsed, min_value), max_value)


def _parse_bounded_int(raw: str, default: int, *, min_value: int, max_value: int) -> int:
    value = (raw or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return min(max(parsed, min_value), max_value)


def _parse_csv_list(raw: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in (raw or "").split(","):
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        values.append(cleaned)
    return values


def _compare_pack_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": "custom",
            "label": "Custom",
            "description": "Manually pick connection + model per row.",
            "preferred_models": [],
        },
        {
            "id": "budget",
            "label": "Budget",
            "description": "Lowest-cost default mapping for broad testing.",
            "preferred_models": ["gpt-5-nano", "gpt-4.1-nano"],
        },
        {
            "id": "balanced",
            "label": "Balanced",
            "description": "Quality and latency balance for production.",
            "preferred_models": ["gpt-5-mini", "gpt-4.1-mini"],
        },
        {
            "id": "quality",
            "label": "Quality",
            "description": "Higher-quality responses for critical flows.",
            "preferred_models": ["gpt-5", "gpt-4.1"],
        },
        {
            "id": "omni",
            "label": "Omni",
            "description": "Multimodal-capable compare set.",
            "preferred_models": ["gpt-4o", "gpt-4o-mini"],
        },
    ]


def _provider_models_for_catalog(catalog: list[Any]) -> dict[str, list[str]]:
    provider_models: dict[str, list[str]] = {}
    for provider in catalog:
        models = list_provider_models(provider.slug)
        if not models:
            models = list(provider.recommended_models)
        provider_models[provider.slug] = models
    return provider_models


def _default_test_routes(
    *,
    ready_connections: list[Any],
    provider_catalog: list[Any],
    compare_pack: str = "budget",
) -> list[dict[str, str]]:
    provider_models = _provider_models_for_catalog(provider_catalog)
    pack_lookup = {item["id"]: item for item in _compare_pack_catalog()}
    preferred_models = list(pack_lookup.get(compare_pack, pack_lookup["budget"])["preferred_models"])

    routes: list[dict[str, str]] = []
    used_providers: set[str] = set()
    for connection in ready_connections:
        provider_slug = connection.provider_slug
        if provider_slug in used_providers:
            continue
        models = provider_models.get(provider_slug, [])
        if not models:
            continue
        selected_model = next((item for item in preferred_models if item in models), models[0])
        routes.append(
            {
                "provider_slug": provider_slug,
                "provider_config_id": connection.id,
                "model": selected_model,
            }
        )
        used_providers.add(provider_slug)
    return routes[:6]


def _parse_test_routes_json(
    *,
    raw: str,
    ready_connections: list[Any],
    provider_models: dict[str, list[str]],
) -> tuple[list[dict[str, str]], list[str]]:
    errors: list[str] = []
    ready_by_id = {connection.id: connection for connection in ready_connections}
    routes: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    payload = _parse_json(raw, [])
    if not isinstance(payload, list):
        return [], ["Invalid route payload format."]

    for item in payload:
        if not isinstance(item, dict):
            continue
        provider_config_id = str(item.get("provider_config_id", "")).strip()
        model = str(item.get("model", "")).strip()
        provider_slug_raw = str(item.get("provider_slug", "")).strip()
        if not provider_config_id or not model:
            continue

        connection = ready_by_id.get(provider_config_id)
        if connection is None:
            errors.append(f"Connection '{provider_config_id}' is not ready.")
            continue

        provider_slug = connection.provider_slug
        if provider_slug_raw:
            try:
                normalized = ensure_supported_provider_slug(provider_slug_raw)
            except ValueError:
                errors.append(f"Unsupported provider '{provider_slug_raw}'.")
                continue
            if normalized != provider_slug:
                errors.append(
                    f"Connection '{connection.name}' does not match provider '{provider_slug_raw}'."
                )
                continue

        known_models = provider_models.get(provider_slug, [])
        if model not in known_models:
            errors.append(f"Model '{model}' is not in the catalog for provider '{provider_slug}'.")
            continue

        key = (provider_slug, provider_config_id, model)
        if key in seen:
            continue
        seen.add(key)
        routes.append(
            {
                "provider_slug": provider_slug,
                "provider_config_id": provider_config_id,
                "model": model,
            }
        )

    return routes[:6], errors


def _next_auto_target_name(db: Session, tenant_id: str, provider_slug: str, model: str) -> str:
    stem = f"auto-{provider_slug}-{model}".lower().replace("_", "-").replace(".", "-").replace("/", "-")
    candidate = stem
    suffix = 1
    while db.scalar(select(Target.id).where(Target.tenant_id == tenant_id, Target.name == candidate)) is not None:
        suffix += 1
        candidate = f"{stem}-{suffix}"
    return candidate


def _resolve_model_identifier(model_value: str, custom_value: str) -> str:
    selected = (model_value or "").strip()
    if selected == "__custom__":
        return (custom_value or "").strip()
    return selected


def _safe_next_path(raw: str) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    return value


def _api_detail_path(endpoint_id: str) -> str:
    return f"/apis/{endpoint_id}"


def _apis_index_path() -> str:
    return "/apis"


def _batch_metadata(job: Job) -> dict[str, Any]:
    request_json = job.request_json if isinstance(job.request_json, dict) else {}
    metadata = request_json.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    return metadata


def _setup_steps_for_tenant(
    db: Session,
    tenant_id: str,
    *,
    active_provider_count: int,
    active_endpoint_count: int,
    api_key_count: int,
    all_job_count: int,
    training_event_count: int,
) -> tuple[list[dict[str, Any]], int]:
    steps = [
        {
            "title": "Connect provider",
            "description": "Configure platform or tenant auth in Providers.",
            "href": "/providers",
            "done": active_provider_count > 0,
        },
        {
            "title": "Create API",
            "description": "Create at least one API and activate a live version.",
            "href": "/apis",
            "done": active_endpoint_count > 0,
        },
        {
            "title": "Create API key",
            "description": "Issue a scoped key for client calls.",
            "href": "/api-keys",
            "done": api_key_count > 0,
        },
        {
            "title": "Run first job",
            "description": "Submit via public API and inspect result.",
            "href": "/runs",
            "done": all_job_count > 0,
        },
        {
            "title": "Save training data",
            "description": "Capture feedback and export JSONL.",
            "href": "/training",
            "done": training_event_count > 0,
        },
    ]
    setup_done = len([step for step in steps if step["done"]])
    return steps, setup_done


@router.get("/")
def root(
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    if session_user is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    if session_user is not None:
        return RedirectResponse("/dashboard", status_code=303)
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "login.html",
        _base_context(
            request=request,
            db=db,
            session_user=None,
            active_nav="",
            error=request.query_params.get("error"),
            oidc_enabled=settings.oidc_enabled(),
            oidc_issuer_url=settings.oidc_issuer_url,
            local_auth_enabled=settings.local_auth_enabled,
            local_auth_username=settings.local_auth_username,
        ),
    )


@router.get("/login/start")
def login_start(
    request: Request,
    next: str | None = None,
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if not settings.oidc_enabled():
        return templates.TemplateResponse(
            "login.html",
            _base_context(
                request=request,
                db=db,
                session_user=None,
                active_nav="",
                error="OIDC is not configured. Set OIDC_ISSUER_URL and OIDC_CLIENT_ID.",
                oidc_enabled=False,
                oidc_issuer_url=settings.oidc_issuer_url,
            ),
            status_code=503,
        )

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    code_verifier = generate_code_verifier()
    code_challenge = build_code_challenge(code_verifier)

    request.session["oidc_state"] = state
    request.session["oidc_nonce"] = nonce
    request.session["oidc_code_verifier"] = code_verifier
    request.session["oidc_next"] = sanitize_next_path(next)

    try:
        authorization_url = build_authorization_url(state=state, nonce=nonce, code_challenge=code_challenge)
    except OidcAuthError as exc:
        return templates.TemplateResponse(
            "login.html",
            _base_context(
                request=request,
                db=db,
                session_user=None,
                active_nav="",
                error=str(exc),
                oidc_enabled=settings.oidc_enabled(),
                oidc_issuer_url=settings.oidc_issuer_url,
            ),
            status_code=503,
        )

    return RedirectResponse(authorization_url, status_code=303)


@router.post("/login/local")
def login_local(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if not settings.local_auth_enabled:
        return RedirectResponse("/login?error=Local+test+login+is+disabled", status_code=303)

    expected_username = settings.local_auth_username.strip()
    expected_password = settings.local_auth_password

    # Backward-compatible bootstrap login path controlled by env credentials.
    if username.strip() == expected_username and password == expected_password:
        user, tenant = _ensure_local_auth_principal(db)
        if hasattr(user, "last_login_at"):
            user.last_login_at = datetime.now(UTC)
        _assign_session_user(request, user, tenant)
        return RedirectResponse("/dashboard", status_code=303)

    tenant = db.scalar(select(Tenant).where(Tenant.name == settings.default_tenant_name))
    if tenant is None:
        tenant = Tenant(
            name=settings.default_tenant_name,
            can_create_subtenants=True,
            inherit_provider_configs=True,
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

    identifier = username.strip().lower()
    if not identifier:
        return RedirectResponse("/login?error=Invalid+local+test+credentials", status_code=303)

    if "@" in identifier:
        email = identifier
    elif identifier == expected_username and settings.local_auth_email.strip():
        email = settings.local_auth_email.strip().lower()
    else:
        # Local login is email-based for tenant-managed users.
        email = identifier

    user = db.scalar(select(User).where(User.tenant_id == tenant.id, User.email == email))
    if user is None or not verify_password(password, user.password_hash):
        return RedirectResponse("/login?error=Invalid+local+test+credentials", status_code=303)

    user.last_login_at = datetime.now(UTC)
    db.add(user)
    db.commit()
    db.refresh(user)
    _assign_session_user(request, user, tenant)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/auth/callback")
def oidc_callback(
    request: Request,
    db: Session = Depends(get_db),
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    if error:
        message = error_description or error
        return RedirectResponse(f"/login?error={quote_plus(message)}", status_code=303)

    expected_state = request.session.pop("oidc_state", None)
    expected_nonce = request.session.pop("oidc_nonce", None)
    code_verifier = request.session.pop("oidc_code_verifier", None)
    next_path = sanitize_next_path(request.session.pop("oidc_next", None))

    if not expected_state or not state or state != expected_state:
        return RedirectResponse("/login?error=Invalid+OIDC+state", status_code=303)
    if not code:
        return RedirectResponse("/login?error=Missing+authorization+code", status_code=303)
    if not code_verifier:
        return RedirectResponse("/login?error=Missing+PKCE+verifier+in+session", status_code=303)

    try:
        tokens = exchange_code_for_tokens(code=code, code_verifier=code_verifier)
        claims = parse_and_validate_id_token(tokens["id_token"], expected_nonce=expected_nonce)
        user, tenant = provision_user_from_claims(db, claims)
    except OidcAuthError as exc:
        return RedirectResponse(f"/login?error={quote_plus(str(exc))}", status_code=303)

    request.session.clear()
    request.session["user_id"] = user.id
    request.session["tenant_id"] = tenant.id
    request.session["principal_tenant_id"] = tenant.id
    request.session["active_tenant_id"] = tenant.id
    request.session["role"] = user.role.value
    request.session["email"] = user.email
    request.session["display_name"] = user.display_name
    request.session["id_token"] = tokens.get("id_token")
    return RedirectResponse(next_path, status_code=303)


@router.post("/logout")
def logout_submit(request: Request):
    id_token_hint = request.session.get("id_token")
    request.session.clear()
    try:
        target = build_logout_url(id_token_hint=id_token_hint)
    except OidcAuthError:
        target = "/login"
    return RedirectResponse(target, status_code=303)


@router.post("/tenant-context/switch")
def switch_tenant_context(
    request: Request,
    tenant_id: str = Form(...),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    if not is_same_or_descendant(db, session_user.principal_tenant_id, tenant_id):
        return RedirectResponse("/dashboard", status_code=303)

    request.session["active_tenant_id"] = tenant_id
    request.session["tenant_id"] = tenant_id
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    since = datetime.now(UTC) - timedelta(hours=24)
    queued = db.scalar(select(func.count(Job.id)).where(Job.tenant_id == session_user.tenant_id, Job.status == JobStatus.queued))
    running = db.scalar(select(func.count(Job.id)).where(Job.tenant_id == session_user.tenant_id, Job.status == JobStatus.running))
    completed = db.scalar(
        select(func.count(Job.id)).where(
            Job.tenant_id == session_user.tenant_id,
            Job.status == JobStatus.completed,
            Job.created_at >= since,
        )
    )
    failed = db.scalar(
        select(func.count(Job.id)).where(
            Job.tenant_id == session_user.tenant_id,
            Job.status == JobStatus.failed,
            Job.created_at >= since,
        )
    )
    cache_hits = db.scalar(
        select(func.count(Job.id)).where(
            Job.tenant_id == session_user.tenant_id,
            Job.cache_hit.is_(True),
            Job.created_at >= since,
        )
    )
    total_jobs_24h = db.scalar(select(func.count(Job.id)).where(Job.tenant_id == session_user.tenant_id, Job.created_at >= since))
    spend_last_24h = db.scalar(
        select(func.coalesce(func.sum(Job.estimated_cost_usd), 0.0)).where(
            Job.tenant_id == session_user.tenant_id,
            Job.created_at >= since,
        )
    )
    spend_all_time = db.scalar(
        select(func.coalesce(func.sum(Job.estimated_cost_usd), 0.0)).where(Job.tenant_id == session_user.tenant_id)
    )

    provider_configs = list_tenant_provider_configs(db, session_user.tenant_id)
    active_provider_count = len([cfg for cfg in provider_configs if cfg.is_active])
    endpoint_count = db.scalar(select(func.count(Endpoint.id)).where(Endpoint.tenant_id == session_user.tenant_id)) or 0
    active_endpoint_count = (
        db.scalar(
            select(func.count(Endpoint.id)).where(
                Endpoint.tenant_id == session_user.tenant_id,
                Endpoint.active_version_id.is_not(None),
            )
        )
        or 0
    )
    api_key_count = (
        db.scalar(
            select(func.count(ApiKey.id)).where(
                ApiKey.tenant_id == session_user.tenant_id,
                ApiKey.is_active.is_(True),
            )
        )
        or 0
    )
    all_job_count = db.scalar(select(func.count(Job.id)).where(Job.tenant_id == session_user.tenant_id)) or 0
    training_event_count = (
        db.scalar(select(func.count(TrainingEvent.id)).where(TrainingEvent.tenant_id == session_user.tenant_id)) or 0
    )

    setup_steps, setup_done = _setup_steps_for_tenant(
        db,
        session_user.tenant_id,
        active_provider_count=active_provider_count,
        active_endpoint_count=active_endpoint_count,
        api_key_count=api_key_count,
        all_job_count=all_job_count,
        training_event_count=training_event_count,
    )
    queue = get_queue()
    endpoints = db.scalars(
        select(Endpoint).where(Endpoint.tenant_id == session_user.tenant_id).order_by(Endpoint.created_at.desc())
    ).all()
    endpoint_ids = [item.id for item in endpoints]
    versions_by_id: dict[str, EndpointVersion] = {}
    if endpoint_ids:
        versions = db.scalars(select(EndpointVersion).where(EndpointVersion.endpoint_id.in_(endpoint_ids))).all()
        versions_by_id = {version.id: version for version in versions}

    ready_provider_slugs = {item.slug for item in list_ready_provider_catalog_for_tenant(db, session_user.tenant_id)}
    targets = list_targets(db, session_user.tenant_id)
    target_by_id = {target.id: target for target in targets}

    endpoint_health: list[dict[str, Any]] = []
    attention_items: list[dict[str, str]] = []
    jobs_24h_all = db.scalars(
        select(Job).where(Job.tenant_id == session_user.tenant_id, Job.created_at >= since).order_by(Job.created_at.desc())
    ).all()
    batch_jobs_24h = len([job for job in jobs_24h_all if job.provider_batch_run_id is not None])
    batch_runs_24h = (
        db.scalar(
            select(func.count(ProviderBatchRun.id)).where(
                ProviderBatchRun.tenant_id == session_user.tenant_id,
                ProviderBatchRun.created_at >= since,
            )
        )
        or 0
    )
    jobs_by_endpoint: dict[str, list[Job]] = {}
    for job in jobs_24h_all:
        jobs_by_endpoint.setdefault(job.endpoint_id, []).append(job)

    for endpoint in endpoints:
        live_version = versions_by_id.get(endpoint.active_version_id or "")
        endpoint_jobs = jobs_by_endpoint.get(endpoint.id, [])
        total = len(endpoint_jobs)
        failed_count = len([job for job in endpoint_jobs if job.status == JobStatus.failed])
        failure_rate = (failed_count / total * 100.0) if total > 0 else 0.0

        live_label = "No live version"
        provider_model = "-"
        status = "warning" if endpoint.active_version_id is None else "ok"
        status_reason = "No live version assigned."

        if live_version is not None:
            live_label = f"v{live_version.version}"
            provider_model = f"{live_version.provider}/{live_version.model}"
            status = "ok"
            status_reason = "Healthy."
            if live_version.provider not in ready_provider_slugs:
                status = "warning"
                status_reason = f"Provider '{live_version.provider}' is not ready."
            elif live_version.target_id:
                target = target_by_id.get(live_version.target_id)
                if target is None:
                    status = "warning"
                    status_reason = "Deployment is missing."
                elif not target.is_active:
                    status = "warning"
                    status_reason = f"Deployment '{target.name}' is disabled."
                elif not target.is_verified:
                    status = "warning"
                    status_reason = f"Deployment '{target.name}' is not verified."

        if endpoint.active_version_id is None:
            attention_items.append(
                {"title": endpoint.name, "detail": "No live version. Open API and activate one.", "href": _api_detail_path(endpoint.id)}
            )
        elif total >= 10 and failure_rate >= 10.0:
            attention_items.append(
                {
                    "title": endpoint.name,
                    "detail": f"High failure rate in last 24h ({failure_rate:.1f}%).",
                    "href": f"/runs?endpoint_id={endpoint.id}",
                }
            )

        endpoint_health.append(
            {
                "id": endpoint.id,
                "name": endpoint.name,
                "live_version": live_label,
                "provider_model": provider_model,
                "jobs_24h": total,
                "failed_24h": failed_count,
                "failure_rate": failure_rate,
                "status": status,
                "status_reason": status_reason,
                "href": _api_detail_path(endpoint.id),
            }
        )

    if api_key_count == 0:
        attention_items.append({"title": "API keys", "detail": "No active API keys. Create one to call APIs.", "href": "/api-keys"})
    if active_provider_count == 0:
        attention_items.append({"title": "Providers", "detail": "No active provider connections.", "href": "/providers"})

    latencies_ms: list[float] = []
    for job in jobs_24h_all:
        if job.started_at and job.finished_at:
            delta = (job.finished_at - job.started_at).total_seconds() * 1000.0
            if delta >= 0:
                latencies_ms.append(delta)
    latencies_ms.sort()
    p95_latency_ms = 0.0
    avg_latency_ms = 0.0
    if latencies_ms:
        p95_index = max(0, int(len(latencies_ms) * 0.95) - 1)
        p95_latency_ms = float(latencies_ms[p95_index])
        avg_latency_ms = float(statistics.fmean(latencies_ms))
    error_rate_24h = float((failed or 0) / (total_jobs_24h or 1) * 100.0) if (total_jobs_24h or 0) > 0 else 0.0
    cache_hit_rate_24h = float((cache_hits or 0) / (total_jobs_24h or 1) * 100.0) if (total_jobs_24h or 0) > 0 else 0.0

    recent_changes = db.scalars(
        select(AuditLog)
        .where(AuditLog.tenant_id == session_user.tenant_id)
        .order_by(AuditLog.created_at.desc())
        .limit(8)
    ).all()

    return templates.TemplateResponse(
        "dashboard.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="dashboard",
            queued_jobs=queued or 0,
            running_jobs=running or 0,
            completed_last_24h=completed or 0,
            failed_last_24h=failed or 0,
            cache_hits_last_24h=cache_hits or 0,
            jobs_last_24h=total_jobs_24h or 0,
            spend_last_24h=float(spend_last_24h or 0.0),
            spend_all_time=float(spend_all_time or 0.0),
            queue_size=queue.count,
            active_provider_count=active_provider_count,
            endpoint_count=endpoint_count,
            active_endpoint_count=active_endpoint_count,
            live_endpoint_count=active_endpoint_count,
            api_key_count=api_key_count,
            all_job_count=all_job_count,
            training_event_count=training_event_count,
            batch_jobs_24h=batch_jobs_24h,
            batch_runs_24h=batch_runs_24h,
            p95_latency_ms=p95_latency_ms,
            avg_latency_ms=avg_latency_ms,
            error_rate_24h=error_rate_24h,
            cache_hit_rate_24h=cache_hit_rate_24h,
            endpoint_health=endpoint_health,
            attention_items=attention_items[:8],
            recent_changes=recent_changes,
            setup_steps=setup_steps,
            setup_done=setup_done,
            role=session_user.role,
        ),
    )


@router.get("/setup-guide", response_class=HTMLResponse)
def setup_guide_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    since = datetime.now(UTC) - timedelta(hours=24)
    provider_configs = list_tenant_provider_configs(db, session_user.tenant_id)
    active_provider_count = len([cfg for cfg in provider_configs if cfg.is_active])
    active_endpoint_count = (
        db.scalar(
            select(func.count(Endpoint.id)).where(
                Endpoint.tenant_id == session_user.tenant_id,
                Endpoint.active_version_id.is_not(None),
            )
        )
        or 0
    )
    api_key_count = (
        db.scalar(
            select(func.count(ApiKey.id)).where(
                ApiKey.tenant_id == session_user.tenant_id,
                ApiKey.is_active.is_(True),
            )
        )
        or 0
    )
    all_job_count = db.scalar(select(func.count(Job.id)).where(Job.tenant_id == session_user.tenant_id)) or 0
    training_event_count = (
        db.scalar(select(func.count(TrainingEvent.id)).where(TrainingEvent.tenant_id == session_user.tenant_id)) or 0
    )
    setup_steps, setup_done = _setup_steps_for_tenant(
        db,
        session_user.tenant_id,
        active_provider_count=active_provider_count,
        active_endpoint_count=active_endpoint_count,
        api_key_count=api_key_count,
        all_job_count=all_job_count,
        training_event_count=training_event_count,
    )
    jobs_last_24h = (
        db.scalar(select(func.count(Job.id)).where(Job.tenant_id == session_user.tenant_id, Job.created_at >= since))
        or 0
    )

    return templates.TemplateResponse(
        "setup_guide.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="setup-guide",
            setup_steps=setup_steps,
            setup_done=setup_done,
            jobs_last_24h=jobs_last_24h,
        ),
    )


@router.get("/developers", response_class=HTMLResponse)
def developers_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    provider_items = list_provider_catalog()
    providers: list[dict[str, Any]] = []
    for provider in provider_items:
        models = list_provider_models(provider.slug)
        providers.append(
            {
                "slug": provider.slug,
                "name": provider.name,
                "logo_path": provider.logo_path,
                "docs_url": provider.docs_url,
                "realtime_docs_url": provider.realtime_docs_url,
                "default_model": provider.default_model,
                "models_preview": models[:8],
                "model_count": len(models),
            }
        )

    sample_endpoint_id = "YOUR_ENDPOINT_ID"
    sample_api_key = "fc_xxx"
    sample_job_id = "job_xxx"
    runs_last_24h = 0
    doc_stats = {
        "endpoint_count": 0,
        "active_key_count": 0,
    }
    if session_user is not None:
        candidate_endpoint = db.scalar(
            select(Endpoint)
            .where(Endpoint.tenant_id == session_user.tenant_id)
            .order_by(Endpoint.created_at.desc())
        )
        if candidate_endpoint:
            sample_endpoint_id = candidate_endpoint.id
        candidate_key_prefix = db.scalar(
            select(ApiKey.key_prefix)
            .where(
                ApiKey.tenant_id == session_user.tenant_id,
                ApiKey.is_active.is_(True),
            )
            .order_by(ApiKey.created_at.desc())
        )
        if candidate_key_prefix:
            sample_api_key = f"{candidate_key_prefix}..."

        candidate_job_id = db.scalar(
            select(Job.id)
            .where(Job.tenant_id == session_user.tenant_id)
            .order_by(Job.created_at.desc())
        )
        if candidate_job_id:
            sample_job_id = candidate_job_id

        since = datetime.now(UTC) - timedelta(hours=24)
        runs_last_24h = (
            db.scalar(
                select(func.count(Job.id)).where(
                    Job.tenant_id == session_user.tenant_id,
                    Job.created_at >= since,
                )
            )
            or 0
        )
        doc_stats["endpoint_count"] = (
            db.scalar(select(func.count(Endpoint.id)).where(Endpoint.tenant_id == session_user.tenant_id)) or 0
        )
        doc_stats["active_key_count"] = (
            db.scalar(
                select(func.count(ApiKey.id)).where(
                    ApiKey.tenant_id == session_user.tenant_id,
                    ApiKey.is_active.is_(True),
                )
            )
            or 0
        )

    public_api_reference = [
        {
            "method": "POST",
            "path": "/v1/endpoints/{endpoint_id}/jobs",
            "auth": "x-api-key",
            "purpose": "Queue an async run and return a job_id immediately.",
        },
        {
            "method": "POST",
            "path": "/v1/endpoints/{endpoint_id}/responses",
            "auth": "x-api-key",
            "purpose": "Run inline and return completed/failed job payload in one call.",
        },
        {
            "method": "GET",
            "path": "/v1/jobs/{job_id}",
            "auth": "x-api-key",
            "purpose": "Poll async run status and fetch output/usage/error.",
        },
        {
            "method": "POST",
            "path": "/v1/jobs/{job_id}/cancel",
            "auth": "x-api-key",
            "purpose": "Request cancellation for queued/running job.",
        },
        {
            "method": "POST",
            "path": "/v1/jobs/{job_id}/save",
            "auth": "x-api-key or session",
            "purpose": "Persist feedback/ideal output into training dataset.",
        },
        {
            "method": "POST",
            "path": "/v1/endpoints/{endpoint_id}/batches",
            "auth": "x-api-key",
            "purpose": "Submit provider-native async batches (OpenAI/Azure).",
        },
        {
            "method": "GET",
            "path": "/v1/batches/{batch_id}",
            "auth": "x-api-key",
            "purpose": "Poll provider-native batch run status and progress.",
        },
        {
            "method": "POST",
            "path": "/v1/batches/{batch_id}/cancel",
            "auth": "x-api-key",
            "purpose": "Cancel a submitted provider-native batch run.",
        },
    ]

    sdk_reference = [
        {
            "method": "submit_job(endpoint_id, input_text|messages, ...)",
            "purpose": "Queue a single async run using the public API contract.",
        },
        {
            "method": "create_response(endpoint_id, input_text|messages, ...)",
            "purpose": "Run a single request inline and get full result in one SDK call.",
        },
        {"method": "get_job(job_id)", "purpose": "Fetch current job state snapshot."},
        {"method": "wait_for_job(job_id, ...)", "purpose": "Poll job until terminal state."},
        {
            "method": "submit_batch(endpoint_id, items|inputs, service_tier=...)",
            "purpose": "Queue provider-native async batch run (OpenAI/Azure).",
        },
        {"method": "wait_for_batch(batch_id, ...)", "purpose": "Poll batch until terminal state."},
        {"method": "save_training(job_id, ...)", "purpose": "Store feedback/few-shot training event."},
        {"method": "submit_and_wait(endpoint_id, ...)", "purpose": "Synchronous helper built on async runs."},
    ]

    error_reference = [
        {
            "code": "400",
            "cause": "Invalid payload or unsupported model params.",
            "fix": "Validate request body and model parameter set before submit.",
        },
        {
            "code": "401",
            "cause": "Missing or invalid API key.",
            "fix": "Pass a valid active key in the x-api-key header.",
        },
        {
            "code": "403",
            "cause": "API key scope does not allow this endpoint/job/batch.",
            "fix": "Regenerate key with endpoint scope or all endpoints scope.",
        },
        {
            "code": "404",
            "cause": "Endpoint, job, or batch id does not exist for this tenant.",
            "fix": "Check tenant context and identifiers used by the caller.",
        },
        {
            "code": "429",
            "cause": "Rate-limit or monthly quota reached for API key.",
            "fix": "Increase limits or wait for next quota window.",
        },
    ]

    onboarding_steps = [
        {
            "title": "Connect at least one provider",
            "href": "/providers",
            "done": bool(session_user and list_ready_provider_connections_for_tenant(db, session_user.tenant_id)),
        },
        {
            "title": "Create an API and activate a version",
            "href": "/apis",
            "done": doc_stats["endpoint_count"] > 0,
        },
        {
            "title": "Create API key scoped to that API",
            "href": "/api-keys",
            "done": doc_stats["active_key_count"] > 0,
        },
        {
            "title": "Run first async request",
            "href": "/playground",
            "done": runs_last_24h > 0,
        },
    ]

    return templates.TemplateResponse(
        "developers.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="developers",
            sample_endpoint_id=sample_endpoint_id,
            sample_api_key=sample_api_key,
            sample_job_id=sample_job_id,
            providers=providers,
            public_api_reference=public_api_reference,
            sdk_reference=sdk_reference,
            error_reference=error_reference,
            onboarding_steps=onboarding_steps,
            runs_last_24h=runs_last_24h,
            doc_stats=doc_stats,
        ),
    )


@router.get("/sdk-readme", response_class=PlainTextResponse)
def sdk_readme() -> PlainTextResponse:
    sdk_readme_path = Path(__file__).resolve().parents[3] / "sdk" / "README.md"
    if not sdk_readme_path.exists():
        return PlainTextResponse("SDK README not found.", status_code=404)
    return PlainTextResponse(sdk_readme_path.read_text(encoding="utf-8"))


def _playground_endpoint_rows(db: Session, tenant_id: str) -> list[dict[str, Any]]:
    endpoints = db.scalars(select(Endpoint).where(Endpoint.tenant_id == tenant_id).order_by(Endpoint.created_at.desc())).all()
    rows: list[dict[str, Any]] = []
    for endpoint in endpoints:
        active_version = get_active_version(db, endpoint)
        rows.append(
            {
                "id": endpoint.id,
                "name": endpoint.name,
                "description": endpoint.description,
                "active_version_id": endpoint.active_version_id,
                "provider": active_version.provider if active_version else None,
                "model": active_version.model if active_version else None,
                "has_active_version": active_version is not None,
            }
        )
    return rows


@router.get("/playground", response_class=HTMLResponse)
def playground_page(
    request: Request,
    endpoint_id: str | None = None,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    endpoint_rows = _playground_endpoint_rows(db, session_user.tenant_id)
    key_rows = db.scalars(select(ApiKey).where(ApiKey.tenant_id == session_user.tenant_id).order_by(ApiKey.created_at.desc())).all()

    selected_endpoint_id = (endpoint_id or "").strip()
    endpoint_lookup = {row["id"]: row for row in endpoint_rows}
    if selected_endpoint_id not in endpoint_lookup:
        live = next((row for row in endpoint_rows if row["has_active_version"]), None)
        selected_endpoint_id = live["id"] if live else (endpoint_rows[0]["id"] if endpoint_rows else "")

    flash_message = request.session.pop("playground_flash", None)
    return templates.TemplateResponse(
        "playground.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="playground",
            flash_message=flash_message,
            endpoint_rows=endpoint_rows,
            selected_endpoint_id=selected_endpoint_id,
            endpoint_rows_json=json.dumps(endpoint_rows),
            key_rows=[
                {
                    "id": item.id,
                    "name": item.name,
                    "key_prefix": item.key_prefix,
                    "is_active": item.is_active,
                    "scopes": item.scopes or {"all": True},
                }
                for item in key_rows
            ],
        ),
    )


@router.post("/playground/submit")
def playground_submit(
    request: Request,
    endpoint_id: str = Form(...),
    input_text: str = Form(default=""),
    metadata_json: str = Form(default="{}"),
    subtenant_code: str = Form(default=""),
    save_default: str | None = Form(default=None),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == session_user.tenant_id))
    if endpoint is None:
        request.session["playground_flash"] = "API not found for this tenant."
        return RedirectResponse("/playground", status_code=303)

    active_version = get_active_version(db, endpoint)
    if active_version is None:
        request.session["playground_flash"] = "API has no live version. Activate one first."
        return RedirectResponse(f"/playground?endpoint_id={endpoint.id}", status_code=303)

    parsed_metadata = _parse_json(metadata_json, {})
    if not isinstance(parsed_metadata, dict):
        parsed_metadata = {}

    subtenant = subtenant_code.strip() or None
    payload = JobCreateRequest(
        input=input_text.strip() or "Test prompt",
        metadata=parsed_metadata,
        subtenant_code=subtenant,
        save_default=save_default == "on",
    )
    job = create_job(
        db,
        tenant_id=session_user.tenant_id,
        endpoint=endpoint,
        active_version=active_version,
        request_payload=payload,
    )
    queue = get_queue()
    settings = get_settings()
    queue.enqueue("app.tasks.process_job", job.id, job_id=job.id, job_timeout=settings.job_timeout_seconds)

    request.session["playground_flash"] = f"Run queued as {job.id}."
    return RedirectResponse(f"/runs/{job.id}", status_code=303)


@router.post("/playground/dry-run")
async def playground_dry_run(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return JSONResponse(status_code=401, content={"ok": False, "error": "Authentication required"})

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    endpoint_id = str(payload.get("endpoint_id", "")).strip()
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    raw_input = str(payload.get("input", "") or "").strip()
    messages = payload.get("messages")
    if not raw_input and isinstance(messages, list):
        raw_input = collect_request_text(None, messages).strip()

    if not endpoint_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "endpoint_id is required"})
    if not raw_input:
        return JSONResponse(status_code=400, content={"ok": False, "error": "input or messages is required"})

    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == session_user.tenant_id))
    if endpoint is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "API not found"})

    active_version = get_active_version(db, endpoint)
    if active_version is None:
        return JSONResponse(status_code=400, content={"ok": False, "error": "API has no live version"})

    tenant_variables = tenant_variables_map(db, session_user.tenant_id)
    rendered_input, _ = render_job_input(
        input_template=active_version.input_template,
        input_text=raw_input,
        metadata=metadata,
        tenant_variables=tenant_variables,
    )
    effective_input = rendered_input or raw_input

    persona = None
    if active_version.persona_id:
        persona = db.scalar(
            select(Persona).where(Persona.id == active_version.persona_id, Persona.tenant_id == session_user.tenant_id)
        )
    contexts = list_context_blocks_for_version(db, session_user.tenant_id, active_version.id)
    combined_system_prompt = compose_system_prompt(
        system_prompt=active_version.system_prompt,
        persona=persona,
        context_blocks=contexts,
    )

    raw_params = dict(active_version.params_json or {})
    for key in ("enable_fallbacks", "routing_strategy", "fallback_targets", "fallback_models", "max_route_attempts"):
        raw_params.pop(key, None)
    try:
        validated_params = validate_model_params(
            provider_slug=active_version.provider,
            model=active_version.model,
            params=raw_params,
        )
    except ModelParamValidationError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    api_key: str | None = None
    api_base: str | None = None
    api_version: str | None = None
    credential_warning: str | None = None
    try:
        creds = resolve_provider_credentials(
            db,
            tenant_id=session_user.tenant_id,
            provider_slug=active_version.provider,
        )
        api_key = creds.api_key
        api_base = creds.api_base
        api_version = creds.api_version
    except Exception as exc:  # noqa: BLE001
        credential_warning = str(exc)

    advisor = build_token_cost_advisor(
        db=db,
        tenant_id=session_user.tenant_id,
        provider_slug=active_version.provider,
        model=active_version.model,
        api_key=api_key,
        api_base=api_base,
        api_version=api_version,
        system_prompt=combined_system_prompt,
        input_payload=effective_input,
        params=validated_params.params,
        metadata=metadata,
    )

    return {
        "ok": True,
        "endpoint_id": endpoint.id,
        "endpoint_name": endpoint.name,
        "active_version_id": active_version.id,
        "provider": active_version.provider,
        "model": active_version.model,
        "effective_input": effective_input,
        "params": validated_params.params,
        "advisor": advisor,
        "credential_warning": credential_warning,
    }


@router.get("/builder", response_class=HTMLResponse)
def builder_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/dashboard", status_code=303)

    endpoints = db.scalars(
        select(Endpoint).where(Endpoint.tenant_id == session_user.tenant_id).order_by(Endpoint.created_at.desc())
    ).all()
    keys = db.scalars(
        select(ApiKey).where(ApiKey.tenant_id == session_user.tenant_id).order_by(ApiKey.created_at.desc()).limit(20)
    ).all()
    deployments = list_targets(db, session_user.tenant_id)
    ready_connections = list_ready_provider_connections_for_tenant(db, session_user.tenant_id)
    provider_catalog = list_provider_catalog()
    provider_models: dict[str, list[str]] = {}
    provider_params: dict[str, dict[str, Any]] = {}
    for provider in provider_catalog:
        models = list_provider_models(provider.slug)
        if not models:
            models = list(provider.recommended_models)
        provider_models[provider.slug] = models
        provider_params[provider.slug] = {model: get_model_parameters(provider.slug, model) for model in models}
    builder_flash = request.session.pop("builder_flash", None)
    deployment_flash = request.session.pop("target_flash", None)
    provider_flash = request.session.pop("provider_flash", None)
    new_api_key = request.session.pop("new_api_key", None)

    return templates.TemplateResponse(
        "builder.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="builder",
            endpoints=endpoints,
            keys=keys,
            deployments=deployments,
            ready_connections=ready_connections,
            provider_models_json=json.dumps(provider_models),
            provider_params_json=json.dumps(provider_params),
            builder_flash=builder_flash,
            deployment_flash=deployment_flash,
            provider_flash=provider_flash,
            new_api_key=new_api_key,
        ),
    )


@router.get("/usage-costs", response_class=HTMLResponse)
def usage_costs_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    requested_window = request.query_params.get("window_hours", "24")
    requested_limit = request.query_params.get("bucket_limit", "12")
    window_hours = _parse_bounded_int(requested_window, 24, min_value=1, max_value=24 * 365)
    bucket_limit = _parse_bounded_int(requested_limit, 12, min_value=1, max_value=100)
    summary = build_usage_summary(
        db,
        tenant_id=session_user.tenant_id,
        window_hours=window_hours,
        bucket_limit=bucket_limit,
    )
    window_options = [
        {"label": "24h", "hours": 24},
        {"label": "7d", "hours": 24 * 7},
        {"label": "30d", "hours": 24 * 30},
    ]
    return templates.TemplateResponse(
        "usage_costs.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="usage-costs",
            summary=summary,
            window_hours=window_hours,
            bucket_limit=bucket_limit,
            window_options=window_options,
            role=session_user.role,
        ),
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/dashboard", status_code=303)

    tenant = db.scalar(select(Tenant).where(Tenant.id == session_user.tenant_id))
    if tenant is None:
        return RedirectResponse("/dashboard", status_code=303)

    flash_message = request.session.pop("settings_flash", None)
    platform_has_key = bool(get_settings().openai_api_key or platform_key_available("openai"))
    variables = list_tenant_variables(db, session_user.tenant_id)

    return templates.TemplateResponse(
        "settings.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="settings",
            tenant=tenant,
            tenant_has_key=tenant_has_configured_key(tenant),
            platform_has_key=platform_has_key,
            flash_message=flash_message,
            variables=variables,
        ),
    )


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/dashboard", status_code=303)

    users = db.scalars(
        select(User).where(User.tenant_id == session_user.tenant_id).order_by(User.created_at.asc())
    ).all()
    flash_message = request.session.pop("users_flash", None)
    return templates.TemplateResponse(
        "users.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="users",
            users=users,
            flash_message=flash_message,
        ),
    )


@router.post("/users/create")
def users_create(
    request: Request,
    email: str = Form(default=""),
    display_name: str = Form(default=""),
    role: str = Form(default="viewer"),
    password: str = Form(default=""),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/users", status_code=303)

    email_value = email.strip().lower()
    role_value = (role or "").strip().lower()
    if not email_value or "@" not in email_value:
        request.session["users_flash"] = "Valid email is required."
        return RedirectResponse("/users", status_code=303)
    if not _role_assignable_by_actor(session_user.role, role_value):
        request.session["users_flash"] = "You cannot assign that role."
        return RedirectResponse("/users", status_code=303)
    if len(password) < 8:
        request.session["users_flash"] = "Password must be at least 8 characters."
        return RedirectResponse("/users", status_code=303)

    existing = db.scalar(select(User).where(User.tenant_id == session_user.tenant_id, User.email == email_value))
    if existing is not None:
        request.session["users_flash"] = "User already exists for this tenant."
        return RedirectResponse("/users", status_code=303)

    user = User(
        tenant_id=session_user.tenant_id,
        email=email_value,
        display_name=display_name.strip() or None,
        password_hash=hash_password(password),
        role=UserRole(role_value),
    )
    db.add(user)
    db.commit()
    request.session["users_flash"] = f"User '{email_value}' created."
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/update")
def users_update(
    user_id: str,
    request: Request,
    display_name: str = Form(default=""),
    role: str = Form(default="viewer"),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/users", status_code=303)

    user = db.scalar(select(User).where(User.id == user_id, User.tenant_id == session_user.tenant_id))
    if user is None:
        request.session["users_flash"] = "User not found."
        return RedirectResponse("/users", status_code=303)

    requested_role = (role or "").strip().lower()
    if not _role_assignable_by_actor(session_user.role, requested_role):
        request.session["users_flash"] = "You cannot assign that role."
        return RedirectResponse("/users", status_code=303)

    if user.role == UserRole.owner and requested_role != "owner":
        if _count_tenant_owners(db, session_user.tenant_id) <= 1:
            request.session["users_flash"] = "Cannot remove the last owner from tenant."
            return RedirectResponse("/users", status_code=303)

    user.display_name = display_name.strip() or None
    user.role = UserRole(requested_role)
    db.add(user)
    db.commit()
    request.session["users_flash"] = f"User '{user.email}' updated."
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/password")
def users_reset_password(
    user_id: str,
    request: Request,
    password: str = Form(default=""),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/users", status_code=303)

    if len(password) < 8:
        request.session["users_flash"] = "Password must be at least 8 characters."
        return RedirectResponse("/users", status_code=303)

    user = db.scalar(select(User).where(User.id == user_id, User.tenant_id == session_user.tenant_id))
    if user is None:
        request.session["users_flash"] = "User not found."
        return RedirectResponse("/users", status_code=303)
    if session_user.role != "owner" and user.role == UserRole.owner:
        request.session["users_flash"] = "Only owner can reset owner passwords."
        return RedirectResponse("/users", status_code=303)

    user.password_hash = hash_password(password)
    db.add(user)
    db.commit()
    request.session["users_flash"] = f"Password updated for '{user.email}'."
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/delete")
def users_delete(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/users", status_code=303)

    user = db.scalar(select(User).where(User.id == user_id, User.tenant_id == session_user.tenant_id))
    if user is None:
        request.session["users_flash"] = "User not found."
        return RedirectResponse("/users", status_code=303)
    if user.id == session_user.user_id:
        request.session["users_flash"] = "You cannot delete your own account."
        return RedirectResponse("/users", status_code=303)
    if session_user.role != "owner" and user.role == UserRole.owner:
        request.session["users_flash"] = "Only owner can delete owner users."
        return RedirectResponse("/users", status_code=303)
    if user.role == UserRole.owner and _count_tenant_owners(db, session_user.tenant_id) <= 1:
        request.session["users_flash"] = "Cannot delete the last owner from tenant."
        return RedirectResponse("/users", status_code=303)

    db.delete(user)
    db.commit()
    request.session["users_flash"] = "User deleted."
    return RedirectResponse("/users", status_code=303)


@router.get("/tenants", response_class=HTMLResponse)
def tenants_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/dashboard", status_code=303)

    tenants = list_accessible_tenants(db, session_user.principal_tenant_id)
    active_tenant = next((tenant for tenant in tenants if tenant.id == session_user.tenant_id), None)
    effective_query_params_by_tenant = {tenant.id: resolve_effective_query_params(db, tenant.id) for tenant in tenants}
    flash_message = request.session.pop("tenant_flash", None)

    return templates.TemplateResponse(
        "tenants.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="tenants",
            tenants=tenants,
            active_tenant=active_tenant,
            effective_query_params_by_tenant=effective_query_params_by_tenant,
            active_effective_query_params=(
                effective_query_params_by_tenant.get(active_tenant.id, {}) if active_tenant is not None else {}
            ),
            flash_message=flash_message,
        ),
    )


@router.post("/tenants/create")
def tenants_create(
    request: Request,
    name: str = Form(...),
    parent_tenant_id: str = Form(...),
    can_create_subtenants: str | None = Form(default=None),
    inherit_provider_configs: str | None = Form(default=None),
    query_params_mode: str = Form(default="override"),
    query_params_json: str = Form(default="{}"),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/dashboard", status_code=303)

    parent = db.scalar(select(Tenant).where(Tenant.id == parent_tenant_id))
    if parent is None:
        request.session["tenant_flash"] = "Parent tenant not found."
        return RedirectResponse("/tenants", status_code=303)
    if not is_same_or_descendant(db, session_user.principal_tenant_id, parent.id):
        request.session["tenant_flash"] = "Forbidden parent tenant."
        return RedirectResponse("/tenants", status_code=303)
    if not parent.can_create_subtenants and session_user.role != "owner":
        request.session["tenant_flash"] = "Parent tenant does not allow sub-tenant creation."
        return RedirectResponse("/tenants", status_code=303)

    existing = db.scalar(select(Tenant).where(Tenant.name == name.strip()))
    if existing is not None:
        request.session["tenant_flash"] = "Tenant name already exists."
        return RedirectResponse("/tenants", status_code=303)

    parsed_query_params = _parse_json(query_params_json, {})
    if not isinstance(parsed_query_params, dict):
        parsed_query_params = {}

    mode = query_params_mode if query_params_mode in {"inherit", "merge", "override"} else "override"
    tenant = Tenant(
        name=name.strip(),
        parent_tenant_id=parent.id,
        can_create_subtenants=can_create_subtenants == "on",
        inherit_provider_configs=inherit_provider_configs != "off",
        query_params_mode=mode,
        query_params_json=parsed_query_params,
    )
    db.add(tenant)
    db.commit()

    request.session["tenant_flash"] = f"Sub-tenant '{tenant.name}' created."
    return RedirectResponse("/tenants", status_code=303)


@router.post("/tenants/{tenant_id}/update")
def tenants_update(
    tenant_id: str,
    request: Request,
    can_create_subtenants: str | None = Form(default=None),
    inherit_provider_configs: str | None = Form(default=None),
    query_params_mode: str = Form(default="override"),
    query_params_json: str = Form(default="{}"),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/dashboard", status_code=303)

    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None or not is_same_or_descendant(db, session_user.principal_tenant_id, tenant.id):
        request.session["tenant_flash"] = "Tenant not found."
        return RedirectResponse("/tenants", status_code=303)

    parsed_query_params = _parse_json(query_params_json, {})
    if not isinstance(parsed_query_params, dict):
        parsed_query_params = {}

    tenant.can_create_subtenants = can_create_subtenants == "on"
    tenant.inherit_provider_configs = inherit_provider_configs != "off"
    tenant.query_params_mode = query_params_mode if query_params_mode in {"inherit", "merge", "override"} else "override"
    tenant.query_params_json = parsed_query_params
    db.add(tenant)
    db.commit()

    request.session["tenant_flash"] = f"Updated tenant '{tenant.name}'."
    return RedirectResponse("/tenants", status_code=303)


@router.post("/settings/llm")
def settings_update_llm(
    request: Request,
    llm_auth_mode: str = Form(default="platform"),
    openai_api_key: str = Form(default=""),
    clear_tenant_key: str | None = Form(default=None),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/dashboard", status_code=303)

    tenant = db.scalar(select(Tenant).where(Tenant.id == session_user.tenant_id))
    if tenant is None:
        return RedirectResponse("/dashboard", status_code=303)

    settings = get_settings()

    if clear_tenant_key == "on" and tenant.openai_key_ref:
        delete_secret(tenant.openai_key_ref)
        tenant.openai_key_ref = None

    incoming_key = openai_api_key.strip()
    if incoming_key:
        secret_ref = tenant.openai_key_ref or build_openai_secret_ref(tenant.id)
        try:
            put_secret(secret_ref, incoming_key)
        except RuntimeError as exc:
            request.session["settings_flash"] = str(exc)
            return RedirectResponse("/settings", status_code=303)
        tenant.openai_key_ref = secret_ref

    target_mode = LlmAuthMode.platform if llm_auth_mode != "tenant" else LlmAuthMode.tenant
    if target_mode == LlmAuthMode.tenant and not tenant_has_configured_key(tenant):
        request.session["settings_flash"] = "Tenant mode requires a tenant OpenAI API key."
        return RedirectResponse("/settings", status_code=303)

    if target_mode == LlmAuthMode.platform and not (settings.openai_api_key or platform_key_available("openai")):
        request.session["settings_flash"] = "Platform mode requires OPENAI_API_KEY in environment."
        return RedirectResponse("/settings", status_code=303)

    tenant.llm_auth_mode = target_mode
    db.add(tenant)
    db.commit()

    request.session["settings_flash"] = "LLM settings updated."
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/variables/create")
def create_variable(
    key: str = Form(...),
    value: str = Form(default=""),
    is_secret: str | None = Form(default=None),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/settings", status_code=303)

    existing = db.scalar(
        select(TenantVariable).where(TenantVariable.tenant_id == session_user.tenant_id, TenantVariable.key == key.strip())
    )
    if existing is None and key.strip():
        db.add(
            TenantVariable(
                tenant_id=session_user.tenant_id,
                key=key.strip(),
                value=value,
                is_secret=is_secret == "on",
            )
        )
        db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/variables/{variable_id}/update")
def update_variable(
    variable_id: str,
    value: str = Form(default=""),
    is_secret: str | None = Form(default=None),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/settings", status_code=303)

    variable = db.scalar(
        select(TenantVariable).where(TenantVariable.id == variable_id, TenantVariable.tenant_id == session_user.tenant_id)
    )
    if variable:
        variable.value = value
        variable.is_secret = is_secret == "on"
        db.add(variable)
        db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/variables/{variable_id}/delete")
def delete_variable(
    variable_id: str,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/settings", status_code=303)

    variable = db.scalar(
        select(TenantVariable).where(TenantVariable.id == variable_id, TenantVariable.tenant_id == session_user.tenant_id)
    )
    if variable:
        db.delete(variable)
        db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.get("/providers", response_class=HTMLResponse)
def providers_page(
    request: Request,
    provider_slug: str | None = None,
    connection_id: str | None = None,
    open_form: str | None = None,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/dashboard", status_code=303)

    catalog = list_provider_catalog()
    configured = list_tenant_provider_configs(db, session_user.tenant_id)
    selected_slug = None
    if provider_slug:
        normalized = normalize_provider_slug(provider_slug)
        selected_slug = normalized if any(item.slug == normalized for item in catalog) else None
    if selected_slug is None and catalog:
        selected_slug = catalog[0].slug

    selected = get_tenant_provider_config_by_id(db, session_user.tenant_id, connection_id) if connection_id else None
    if selected is not None:
        selected_slug = selected.provider_slug

    provider_source_tenant_ids: dict[str, str | None] = {}
    for provider in catalog:
        _, source_tenant = get_effective_provider_config(db, tenant_id=session_user.tenant_id, provider_slug=provider.slug)
        provider_source_tenant_ids[provider.slug] = source_tenant.id if source_tenant is not None else None

    provider_cards: list[dict[str, Any]] = []
    for provider in catalog:
        provider_configs = [item for item in configured if item.provider_slug == provider.slug]
        provider_cards.append(
            {
                "provider": provider,
                "connections_total": len(provider_configs),
                "connections_ready": len([cfg for cfg in provider_configs if provider_config_is_ready(cfg)]),
                "platform_key_available": platform_key_available(provider.slug),
                "tenant_key_count": len([cfg for cfg in provider_configs if has_tenant_key(cfg)]),
            }
        )

    flash_message = request.session.pop("provider_flash", None)
    provider_connection_fields: dict[str, list[dict[str, Any]]] = {}
    for provider in catalog:
        spec = get_provider_spec(provider.slug)
        fields: list[dict[str, Any]] = []
        if spec is not None:
            for field in spec.connection_fields:
                fields.append(
                    {
                        "key": field.key,
                        "label": field.label,
                        "type": field.field_type,
                        "required": field.required,
                        "placeholder": field.placeholder,
                        "description": field.description,
                    }
                )
        provider_connection_fields[provider.slug] = fields

    selected_field_values: dict[str, Any] = {}
    if selected is not None:
        selected_field_values = dict(selected.extra_json or {})
        normalized_base, normalized_version = resolve_provider_endpoint_options(
            selected.provider_slug,
            api_base=selected.api_base,
            api_version=selected.api_version,
            use_platform_defaults=False,
        )
        if normalized_base:
            selected_field_values["api_base"] = normalized_base
        if normalized_version:
            selected_field_values["api_version"] = normalized_version

    return templates.TemplateResponse(
        "providers.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="providers",
            provider_states=provider_catalog_for_tenant(db, session_user.tenant_id),
            provider_cards=provider_cards,
            configured=configured,
            selected=selected,
            selected_provider_slug=selected_slug,
            open_provider_modal=(open_form == "1" or selected is not None),
            catalog=catalog,
            provider_source_tenant_ids=provider_source_tenant_ids,
            provider_connection_fields_json=json.dumps(provider_connection_fields),
            selected_connection_field_values_json=json.dumps(selected_field_values),
            flash_message=flash_message,
        ),
    )


@router.post("/providers/save")
async def providers_save(
    request: Request,
    provider_slug: str = Form(...),
    provider_config_id: str = Form(default=""),
    connection_name: str = Form(default=""),
    description: str = Form(default=""),
    is_default: str | None = Form(default=None),
    auth_mode: str = Form(default="platform"),
    api_key: str = Form(default=""),
    clear_api_key: str | None = Form(default=None),
    api_base: str = Form(default=""),
    api_version: str = Form(default=""),
    extra_json: str = Form(default="{}"),
    is_active: str | None = Form(default=None),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/providers", status_code=303)

    parsed_extra = _parse_json(extra_json, {})
    if not isinstance(parsed_extra, dict):
        parsed_extra = {}

    try:
        normalized = ensure_supported_provider_slug(provider_slug)
    except ValueError as exc:
        request.session["provider_flash"] = str(exc)
        return RedirectResponse("/providers?open_form=1", status_code=303)

    config_id = provider_config_id.strip()
    existing_config = (
        get_tenant_provider_config_by_id(db, session_user.tenant_id, config_id)
        if config_id
        else None
    )
    if existing_config is not None and existing_config.provider_slug != normalized:
        request.session["provider_flash"] = "Selected connection does not match provider."
        return RedirectResponse(f"/providers?provider_slug={normalized}&open_form=1", status_code=303)

    requested_auth_mode = auth_mode if auth_mode in {"platform", "tenant", "none"} else "platform"
    effective_auth_mode = requested_auth_mode

    incoming_key = api_key.strip()
    entered_base = api_base.strip()
    entered_version = api_version.strip()
    effective_base, effective_version = resolve_provider_endpoint_options(
        normalized,
        api_base=entered_base,
        api_version=entered_version,
        use_platform_defaults=False,
    )

    form_data = await request.form()
    dynamic_fields: dict[str, str] = {}
    for form_key, raw_value in form_data.multi_items():
        if not form_key.startswith("conn_field__"):
            continue
        field_key = form_key.replace("conn_field__", "", 1).strip()
        if not field_key:
            continue
        if hasattr(raw_value, "filename"):
            continue
        dynamic_fields[field_key] = str(raw_value).strip()

    provider_spec = get_provider_spec(normalized)
    required_fields = list(provider_spec.connection_fields) if provider_spec else []

    existing_tenant_key = has_tenant_key(existing_config) if existing_config is not None else False

    for field in required_fields:
        field_value = ""
        if field.key == "api_key":
            field_value = incoming_key
            if not field_value and existing_tenant_key and clear_api_key != "on":
                field_value = "__existing_key__"
        elif field.key == "api_base":
            field_value = effective_base or ""
        elif field.key == "api_version":
            field_value = effective_version or ""
        else:
            field_value = dynamic_fields.get(field.key, "")

        if field.required and not field_value:
            request.session["provider_flash"] = (
                f"Failed to save provider '{normalized}': {field.label} is required."
            )
            return RedirectResponse(f"/providers?provider_slug={normalized}&open_form=1", status_code=303)

    if provider_spec and provider_spec.requires_api_key and effective_auth_mode == "tenant":
        if not incoming_key and not existing_tenant_key:
            request.session["provider_flash"] = (
                f"Failed to save provider '{normalized}': API key is required for tenant auth."
            )
            return RedirectResponse(f"/providers?provider_slug={normalized}&open_form=1", status_code=303)
        if clear_api_key == "on" and not incoming_key:
            request.session["provider_flash"] = (
                f"Failed to save provider '{normalized}': API key is required for tenant auth."
            )
            return RedirectResponse(f"/providers?provider_slug={normalized}&open_form=1", status_code=303)

    merged_extra = dict(parsed_extra)
    for key, value in dynamic_fields.items():
        if key in {"api_key", "api_base", "api_version"}:
            continue
        if value:
            merged_extra[key] = value
        else:
            merged_extra.pop(key, None)

    validation = None
    if incoming_key:
        validation = validate_provider_api_key(
            provider_slug=normalized,
            api_key=incoming_key,
            api_base=effective_base,
            api_version=effective_version,
        )
        if not validation.valid and validation.definitive:
            request.session["provider_flash"] = f"Failed to save provider '{normalized}': {validation.message}"
            return RedirectResponse(f"/providers?provider_slug={normalized}&open_form=1", status_code=303)

    try:
        saved = upsert_tenant_provider_config(
            db,
            tenant_id=session_user.tenant_id,
            provider_slug=normalized,
            provider_config_id=provider_config_id.strip() or None,
            connection_name=connection_name.strip() or None,
            description=description,
            is_default=is_default == "on",
            billing_mode="byok",
            auth_mode=requested_auth_mode,
            api_key=api_key,
            clear_api_key=clear_api_key == "on",
            api_base=effective_base,
            api_version=effective_version,
            extra_json=merged_extra,
            is_active=is_active == "on",
        )
        normalization_notes: list[str] = []
        if is_azure_provider_slug(normalized):
            if entered_base and entered_base != (effective_base or ""):
                normalization_notes.append(f"API base normalized to '{effective_base}'.")
            if not entered_version and effective_version:
                normalization_notes.append(f"API version detected as '{effective_version}'.")
            elif entered_version and effective_version != entered_version:
                normalization_notes.append(f"API version adjusted to '{effective_version}'.")
            elif entered_version and not effective_version:
                normalization_notes.append("API version ignored in /openai/v1 mode.")

        normalization_suffix = f" {' '.join(normalization_notes)}" if normalization_notes else ""
        if incoming_key and validation is not None:
            if validation.definitive:
                if validation.valid:
                    request.session["provider_flash"] = (
                        f"Provider '{normalized}' saved. Key validated.{normalization_suffix}"
                    )
                else:
                    request.session["provider_flash"] = (
                        f"Provider '{normalized}' saved with warning: {validation.message}{normalization_suffix}"
                    )
            else:
                request.session["provider_flash"] = (
                    f"Provider '{normalized}' saved. Validation note: {validation.message}{normalization_suffix}"
                )
        else:
            request.session["provider_flash"] = (
                f"Provider connection '{saved.name}' ({normalized}) saved.{normalization_suffix}"
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Provider connection save failed",
            extra={
                "tenant_id": session_user.tenant_id,
                "provider_slug": normalized,
                "provider_config_id": config_id or None,
            },
        )
        request.session["provider_flash"] = (
            f"Failed to save provider '{normalized}': unable to persist connection. "
            "Check required fields and provider endpoint format."
        )
        return RedirectResponse(
            f"/providers?provider_slug={normalized}&connection_id={config_id}&open_form=1",
            status_code=303,
        )

    return RedirectResponse(f"/providers?provider_slug={normalized}", status_code=303)


@router.post("/providers/{provider_slug}/{provider_config_id}/delete")
def providers_delete(
    provider_slug: str,
    provider_config_id: str,
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/providers", status_code=303)

    try:
        normalized = ensure_supported_provider_slug(provider_slug)
    except ValueError:
        request.session["provider_flash"] = f"Unsupported provider '{provider_slug}'."
        return RedirectResponse("/providers", status_code=303)
    removed = delete_tenant_provider_config(
        db,
        tenant_id=session_user.tenant_id,
        provider_slug=normalized,
        provider_config_id=provider_config_id,
    )
    request.session["provider_flash"] = (
        f"Provider connection removed from '{normalized}'."
        if removed
        else f"Provider connection was not found for '{normalized}'."
    )
    return RedirectResponse(f"/providers?provider_slug={normalized}", status_code=303)


@router.get("/targets", response_class=HTMLResponse)
@router.get("/deployments", response_class=HTMLResponse)
def targets_page(
    request: Request,
    target_id: str | None = None,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/dashboard", status_code=303)

    targets = list_targets(db, session_user.tenant_id)
    selected = next((item for item in targets if item.id == target_id), None)
    flash_message = request.session.pop("target_flash", None)
    provider_catalog = list_provider_catalog()
    ready_connections = list_ready_provider_connections_for_tenant(db, session_user.tenant_id)
    if selected is not None and selected.provider_config_id:
        selected_connection = get_tenant_provider_config_by_id(db, session_user.tenant_id, selected.provider_config_id)
        if selected_connection is not None and all(item.id != selected_connection.id for item in ready_connections):
            ready_connections.insert(0, selected_connection)
    connections_by_provider: dict[str, list[dict[str, Any]]] = {}
    for config in ready_connections:
        connections_by_provider.setdefault(config.provider_slug, []).append(
            {
                "id": config.id,
                "name": config.name,
                "provider_slug": config.provider_slug,
                "is_default": config.is_default,
                "billing_mode": config.billing_mode.value,
            }
        )
    provider_models: dict[str, list[str]] = {}
    provider_params: dict[str, dict[str, Any]] = {}
    provider_equivalents: dict[str, dict[str, Any]] = {}
    for provider in provider_catalog:
        models = list_provider_models(provider.slug)
        if not models:
            models = list(provider.recommended_models)
        provider_models[provider.slug] = models
        provider_params[provider.slug] = {model: get_model_parameters(provider.slug, model) for model in models}
        provider_equivalents[provider.slug] = {
            model: list_equivalent_models(provider.slug, model) for model in models
        }

    return templates.TemplateResponse(
        "targets.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="deployments",
            targets=targets,
            selected=selected,
            catalog=provider_catalog,
            can_create_targets=len(ready_connections) > 0,
            ready_connections=ready_connections,
            provider_connections_json=json.dumps(connections_by_provider),
            provider_models_json=json.dumps(provider_models),
            provider_params_json=json.dumps(provider_params),
            provider_equivalents_json=json.dumps(provider_equivalents),
            provider_defaults_json=json.dumps({item.slug: item.default_model for item in provider_catalog}),
            flash_message=flash_message,
        ),
    )


@router.post("/targets/create")
@router.post("/deployments/create")
def targets_create(
    request: Request,
    name: str = Form(...),
    provider_config_id: str = Form(default=""),
    capability_profile: str = Form(default="responses_chat"),
    model_identifier: str = Form(...),
    model_identifier_custom: str = Form(default=""),
    next_url: str = Form(default=""),
    params_json: str = Form(default="{}"),
    is_active: str | None = Form(default=None),
    verify_now: str | None = Form(default=None),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/deployments", status_code=303)

    cleaned_name = name.strip()
    if not cleaned_name:
        request.session["target_flash"] = "Name is required."
        return RedirectResponse(_safe_next_path(next_url) or "/deployments", status_code=303)
    selected_model_identifier = _resolve_model_identifier(model_identifier, model_identifier_custom)
    if not selected_model_identifier:
        request.session["target_flash"] = "Model is required."
        return RedirectResponse(_safe_next_path(next_url) or "/deployments", status_code=303)

    existing = db.scalar(select(Target.id).where(Target.tenant_id == session_user.tenant_id, Target.name == cleaned_name))
    if existing:
        request.session["target_flash"] = f"Deployment '{cleaned_name}' already exists."
        return RedirectResponse(_safe_next_path(next_url) or "/deployments", status_code=303)

    parsed_params = _parse_json(params_json, {})
    if not isinstance(parsed_params, dict):
        parsed_params = {}

    provider_config = get_tenant_provider_config_by_id(db, session_user.tenant_id, provider_config_id.strip())
    if provider_config is None:
        request.session["target_flash"] = "Selected provider connection was not found."
        return RedirectResponse(_safe_next_path(next_url) or "/deployments", status_code=303)
    if not provider_config_is_ready(provider_config):
        request.session["target_flash"] = "Selected provider connection is not ready. Validate credentials first."
        return RedirectResponse(_safe_next_path(next_url) or "/deployments", status_code=303)

    try:
        payload = TargetCreate(
            name=cleaned_name,
            provider_config_id=provider_config.id,
            provider_slug=provider_config.provider_slug,
            capability_profile=capability_profile,
            model_identifier=selected_model_identifier,
            params_json=parsed_params,
            is_active=is_active == "on",
        )
        target = create_target_record(db, session_user.tenant_id, payload)
    except Exception as exc:  # noqa: BLE001
        request.session["target_flash"] = f"Invalid deployment payload: {exc}"
        return RedirectResponse(_safe_next_path(next_url) or "/deployments", status_code=303)

    if verify_now == "on":
        ok, message = verify_target(db, target)
        request.session["target_flash"] = (
            f"Deployment '{target.name}' created. {message}"
        )
    else:
        request.session["target_flash"] = f"Deployment '{target.name}' created."

    return RedirectResponse(_safe_next_path(next_url) or f"/deployments?target_id={target.id}", status_code=303)


@router.post("/targets/{target_id}/update")
@router.post("/deployments/{target_id}/update")
def targets_update(
    target_id: str,
    request: Request,
    name: str = Form(...),
    provider_config_id: str = Form(default=""),
    capability_profile: str = Form(default="responses_chat"),
    model_identifier: str = Form(...),
    model_identifier_custom: str = Form(default=""),
    next_url: str = Form(default=""),
    params_json: str = Form(default="{}"),
    is_active: str | None = Form(default=None),
    verify_now: str | None = Form(default=None),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/deployments", status_code=303)

    target = get_target(db, session_user.tenant_id, target_id)
    if target is None:
        request.session["target_flash"] = "Deployment not found."
        return RedirectResponse(_safe_next_path(next_url) or "/deployments", status_code=303)

    cleaned_name = name.strip()
    selected_model_identifier = _resolve_model_identifier(model_identifier, model_identifier_custom)
    if not selected_model_identifier:
        request.session["target_flash"] = "Model is required."
        return RedirectResponse(_safe_next_path(next_url) or f"/deployments?target_id={target.id}", status_code=303)
    name_taken = db.scalar(
        select(Target.id).where(
            Target.tenant_id == session_user.tenant_id,
            Target.name == cleaned_name,
            Target.id != target_id,
        )
    )
    if name_taken:
        request.session["target_flash"] = f"Deployment name '{cleaned_name}' is already in use."
        return RedirectResponse(_safe_next_path(next_url) or f"/deployments?target_id={target.id}", status_code=303)

    parsed_params = _parse_json(params_json, {})
    if not isinstance(parsed_params, dict):
        parsed_params = {}

    provider_config = get_tenant_provider_config_by_id(db, session_user.tenant_id, provider_config_id.strip())
    if provider_config is None:
        request.session["target_flash"] = "Selected provider connection was not found."
        return RedirectResponse(_safe_next_path(next_url) or f"/deployments?target_id={target.id}", status_code=303)
    if not provider_config_is_ready(provider_config):
        request.session["target_flash"] = "Selected provider connection is not ready. Validate credentials first."
        return RedirectResponse(_safe_next_path(next_url) or f"/deployments?target_id={target.id}", status_code=303)

    try:
        payload = TargetUpdate(
            name=cleaned_name,
            provider_config_id=provider_config.id,
            provider_slug=provider_config.provider_slug,
            capability_profile=capability_profile,
            model_identifier=selected_model_identifier,
            params_json=parsed_params,
            is_active=is_active == "on",
        )
        updated = update_target_record(db, target, payload)
    except Exception as exc:  # noqa: BLE001
        request.session["target_flash"] = f"Invalid deployment payload: {exc}"
        return RedirectResponse(_safe_next_path(next_url) or f"/deployments?target_id={target.id}", status_code=303)
    if verify_now == "on":
        ok, message = verify_target(db, updated)
        request.session["target_flash"] = (
            f"Deployment '{updated.name}' updated. {message}" if ok else f"Deployment '{updated.name}' updated. {message}"
        )
    else:
        request.session["target_flash"] = f"Deployment '{updated.name}' updated."
    return RedirectResponse(_safe_next_path(next_url) or f"/deployments?target_id={updated.id}", status_code=303)


@router.post("/targets/{target_id}/verify")
@router.post("/deployments/{target_id}/verify")
def targets_verify(
    target_id: str,
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/deployments", status_code=303)

    target = get_target(db, session_user.tenant_id, target_id)
    if target is None:
        request.session["target_flash"] = "Deployment not found."
        return RedirectResponse("/deployments", status_code=303)

    _ok, message = verify_target(db, target)
    request.session["target_flash"] = f"{target.name}: {message}"
    return RedirectResponse(f"/deployments?target_id={target.id}", status_code=303)


@router.post("/targets/{target_id}/delete")
@router.post("/deployments/{target_id}/delete")
def targets_delete(
    target_id: str,
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/deployments", status_code=303)

    target = get_target(db, session_user.tenant_id, target_id)
    if target is None:
        request.session["target_flash"] = "Deployment not found."
        return RedirectResponse("/deployments", status_code=303)

    in_use = db.scalar(select(func.count(EndpointVersion.id)).where(EndpointVersion.target_id == target.id)) or 0
    if in_use > 0:
        request.session["target_flash"] = "Deployment is referenced by endpoint versions and cannot be deleted."
        return RedirectResponse(f"/deployments?target_id={target.id}", status_code=303)

    target_name = target.name
    delete_target_record(db, target)
    request.session["target_flash"] = f"Deployment '{target_name}' deleted."
    return RedirectResponse("/deployments", status_code=303)


@router.get("/pricing", response_class=HTMLResponse)
def pricing_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/dashboard", status_code=303)

    flash_message = request.session.pop("pricing_flash", None) or "Manual pricing is disabled. flash-connector now uses built-in automatic pricing estimates."
    builtin_rates = list_builtin_pricing_rates()

    return templates.TemplateResponse(
        "pricing.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="pricing",
            builtin_rates=builtin_rates,
            flash_message=flash_message,
        ),
    )


@router.get("/apis", response_class=HTMLResponse)
@router.get("/endpoints", response_class=HTMLResponse)
def endpoints_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    endpoints = db.scalars(
        select(Endpoint).where(Endpoint.tenant_id == session_user.tenant_id).order_by(Endpoint.created_at.desc())
    ).all()

    return templates.TemplateResponse(
        "endpoints.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="apis",
            endpoints=endpoints,
        ),
    )


@router.post("/apis/create")
@router.post("/endpoints/create")
def endpoints_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    next_url: str = Form(default=""),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    endpoint = Endpoint(tenant_id=session_user.tenant_id, name=name.strip(), description=description.strip() or None)
    db.add(endpoint)
    db.commit()
    db.refresh(endpoint)
    safe_next = _safe_next_path(next_url)
    if safe_next:
        request.session["builder_flash"] = f"Endpoint '{endpoint.name}' created."
        return RedirectResponse(safe_next, status_code=303)
    return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)


def _endpoint_detail_context(
    *,
    request: Request,
    db: Session,
    session_user: SessionUser,
    endpoint: Endpoint,
    flash_message: str | None = None,
    draft_test_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    versions = db.scalars(
        select(EndpointVersion)
        .where(EndpointVersion.endpoint_id == endpoint.id)
        .order_by(EndpointVersion.version.desc())
    ).all()

    personas = list_personas(db, session_user.tenant_id)
    context_blocks = list_context_blocks(db, session_user.tenant_id)
    all_targets = list_targets(db, session_user.tenant_id)
    provider_catalog = list_ready_provider_catalog_for_tenant(db, session_user.tenant_id)
    ready_provider_slugs = {provider.slug for provider in provider_catalog}
    ready_connections = list_ready_provider_connections_for_tenant(db, session_user.tenant_id)
    ready_connection_ids = {config.id for config in ready_connections}
    available_targets = [
        target
        for target in all_targets
        if target.is_active
        and (
            (target.provider_config_id is not None and target.provider_config_id in ready_connection_ids)
            or (target.provider_config_id is None and target.provider_slug in ready_provider_slugs)
        )
    ]
    provider_models_json = json.dumps(
        {
            item.slug: (list_provider_models(item.slug) or list(item.recommended_models))
            for item in provider_catalog
        }
    )
    provider_params_json = json.dumps(
        {
            item.slug: {
                model: get_model_parameters(item.slug, model)
                for model in (list_provider_models(item.slug) or list(item.recommended_models))
            }
            for item in provider_catalog
        }
    )
    provider_defaults_json = json.dumps({item.slug: item.default_model for item in provider_catalog})
    provider_connections_json = json.dumps(
        {
            provider_slug: [
                {
                    "id": config.id,
                    "name": config.name,
                    "provider_slug": config.provider_slug,
                    "is_default": bool(config.is_default),
                }
                for config in ready_connections
                if config.provider_slug == provider_slug
            ]
            for provider_slug in ready_provider_slugs
        }
    )
    target_options_json = json.dumps(
        {
            target.id: {
                "name": target.name,
                "provider": target.provider_slug,
                "model": target.model_identifier,
                "provider_config_id": target.provider_config_id,
                "is_verified": target.is_verified,
                "is_active": target.is_active,
                "capability_profile": target.capability_profile,
            }
            for target in available_targets
        }
    )
    persona_lookup = {persona.id: persona for persona in personas}
    target_lookup = {target.id: target for target in all_targets}
    context_lookup = {block.id: block for block in context_blocks}

    version_ids = [version.id for version in versions]
    version_context_rows = (
        db.scalars(
            select(EndpointVersionContext)
            .where(EndpointVersionContext.endpoint_version_id.in_(version_ids))
            .order_by(EndpointVersionContext.created_at.asc())
        ).all()
        if version_ids
        else []
    )

    version_context_map: dict[str, list[str]] = {}
    for row in version_context_rows:
        block = context_lookup.get(row.context_block_id)
        if block is None:
            continue
        version_context_map.setdefault(row.endpoint_version_id, []).append(block.name)

    effective_flash_message = flash_message
    if effective_flash_message is None:
        effective_flash_message = request.session.pop("endpoint_flash", None)

    return _base_context(
        request=request,
        db=db,
        session_user=session_user,
        active_nav="apis",
        endpoint=endpoint,
        versions=versions,
        personas=personas,
        context_blocks=context_blocks,
        targets=available_targets,
        provider_catalog=provider_catalog,
        can_create_versions=len(provider_catalog) > 0,
        provider_models_json=provider_models_json,
        provider_params_json=provider_params_json,
        provider_defaults_json=provider_defaults_json,
        provider_connections_json=provider_connections_json,
        target_options_json=target_options_json,
        persona_lookup=persona_lookup,
        target_lookup=target_lookup,
        version_context_map=version_context_map,
        flash_message=effective_flash_message,
        draft_test_result=draft_test_result,
        ready_connections=ready_connections,
    )


@router.get("/apis/{endpoint_id}", response_class=HTMLResponse)
@router.get("/endpoints/{endpoint_id}", response_class=HTMLResponse)
def endpoint_detail(
    endpoint_id: str,
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == session_user.tenant_id))
    if endpoint is None:
        return RedirectResponse(_apis_index_path(), status_code=303)

    return templates.TemplateResponse(
        "endpoint_detail.html",
        _endpoint_detail_context(
            request=request,
            db=db,
            session_user=session_user,
            endpoint=endpoint,
        ),
    )


@router.post("/apis/{endpoint_id}/versions/run-test", response_class=HTMLResponse)
@router.post("/endpoints/{endpoint_id}/versions/run-test", response_class=HTMLResponse)
def endpoint_version_run_test(
    request: Request,
    endpoint_id: str,
    system_prompt: str = Form(...),
    input_template: str = Form(default=""),
    target_id: str = Form(default=""),
    provider: str = Form(default="openai"),
    provider_config_id: str = Form(default=""),
    model: str = Form(default="gpt-5-nano"),
    model_custom: str = Form(default=""),
    persona_id: str = Form(default=""),
    context_block_ids: list[str] = Form(default=[]),
    timeout_seconds: str = Form(default="60"),
    max_retries: str = Form(default="1"),
    params_json: str = Form(default="{}"),
    draft_test_input: str = Form(default=""),
    draft_test_metadata_json: str = Form(default="{}"),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == session_user.tenant_id))
    if endpoint is None:
        return RedirectResponse(_apis_index_path(), status_code=303)

    ready_provider_catalog = list_ready_provider_catalog_for_tenant(db, session_user.tenant_id)
    ready_provider_slugs = {item.slug for item in ready_provider_catalog}
    selected_target_id = target_id.strip() or None
    selected_target = None
    selected_provider_config_id: str | None = None
    if selected_target_id:
        selected_target = get_target(db, session_user.tenant_id, selected_target_id)
        if selected_target is None:
            return templates.TemplateResponse(
                "endpoint_detail.html",
                _endpoint_detail_context(
                    request=request,
                    db=db,
                    session_user=session_user,
                    endpoint=endpoint,
                    flash_message="Selected deployment was not found.",
                ),
            )
        if not selected_target.is_active:
            return templates.TemplateResponse(
                "endpoint_detail.html",
                _endpoint_detail_context(
                    request=request,
                    db=db,
                    session_user=session_user,
                    endpoint=endpoint,
                    flash_message=f"Deployment '{selected_target.name}' is disabled.",
                ),
            )
        if selected_target.provider_slug not in ready_provider_slugs:
            return templates.TemplateResponse(
                "endpoint_detail.html",
                _endpoint_detail_context(
                    request=request,
                    db=db,
                    session_user=session_user,
                    endpoint=endpoint,
                    flash_message=f"Deployment '{selected_target.name}' provider is not ready.",
                ),
            )
        if selected_target.provider_config_id is not None:
            selected_connection = get_tenant_provider_config_by_id(
                db,
                session_user.tenant_id,
                selected_target.provider_config_id,
            )
            if selected_connection is None or not provider_config_is_ready(selected_connection):
                return templates.TemplateResponse(
                    "endpoint_detail.html",
                    _endpoint_detail_context(
                        request=request,
                        db=db,
                        session_user=session_user,
                        endpoint=endpoint,
                        flash_message=f"Deployment '{selected_target.name}' connection is not ready.",
                    ),
                )
            selected_provider_config_id = selected_connection.id
        normalized_provider = selected_target.provider_slug
        selected_model = selected_target.model_identifier
    else:
        selected_model = _resolve_model_identifier(model, model_custom) or "gpt-5-nano"
        normalized_provider = ensure_supported_provider_slug(provider)
        if normalized_provider not in ready_provider_slugs:
            return templates.TemplateResponse(
                "endpoint_detail.html",
                _endpoint_detail_context(
                    request=request,
                    db=db,
                    session_user=session_user,
                    endpoint=endpoint,
                    flash_message=f"Provider '{normalized_provider}' is not ready. Configure it in Providers first.",
                ),
            )
        selected_provider_config_id = provider_config_id.strip() or None
        if selected_provider_config_id is not None:
            selected_connection = get_tenant_provider_config_by_id(
                db,
                session_user.tenant_id,
                selected_provider_config_id,
            )
            if selected_connection is None or not provider_config_is_ready(selected_connection):
                return templates.TemplateResponse(
                    "endpoint_detail.html",
                    _endpoint_detail_context(
                        request=request,
                        db=db,
                        session_user=session_user,
                        endpoint=endpoint,
                        flash_message="Selected provider connection is not ready.",
                    ),
                )
            if selected_connection.provider_slug != normalized_provider:
                return templates.TemplateResponse(
                    "endpoint_detail.html",
                    _endpoint_detail_context(
                        request=request,
                        db=db,
                        session_user=session_user,
                        endpoint=endpoint,
                        flash_message="Selected provider connection does not match provider.",
                    ),
                )

    parsed_metadata = _parse_json(draft_test_metadata_json, {})
    if not isinstance(parsed_metadata, dict):
        parsed_metadata = {}

    raw_input = draft_test_input.strip() or "Test prompt"
    tenant_variables = tenant_variables_map(db, session_user.tenant_id)
    rendered_input, _ = render_job_input(
        input_template=input_template.strip() or None,
        input_text=raw_input,
        metadata=parsed_metadata,
        tenant_variables=tenant_variables,
    )
    effective_input = rendered_input or raw_input

    selected_persona = None
    persona_id_value = persona_id.strip()
    if persona_id_value:
        selected_persona = db.scalar(
            select(Persona).where(Persona.id == persona_id_value, Persona.tenant_id == session_user.tenant_id)
        )

    selected_context_ids = [value.strip() for value in context_block_ids if value and value.strip()]
    selected_context_blocks: list[ContextBlock] = []
    if selected_context_ids:
        context_rows = db.scalars(
            select(ContextBlock).where(
                ContextBlock.tenant_id == session_user.tenant_id,
                ContextBlock.id.in_(selected_context_ids),
            )
        ).all()
        context_map = {block.id: block for block in context_rows}
        selected_context_blocks = [context_map[value] for value in selected_context_ids if value in context_map]

    combined_system_prompt = compose_system_prompt(
        system_prompt=system_prompt,
        persona=selected_persona,
        context_blocks=selected_context_blocks,
    )

    parsed_params = _parse_json(params_json, {})
    if not isinstance(parsed_params, dict):
        parsed_params = {}
    for key in ("enable_fallbacks", "routing_strategy", "fallback_targets", "fallback_models", "max_route_attempts"):
        parsed_params.pop(key, None)
    try:
        validated_params = validate_model_params(
            provider_slug=normalized_provider,
            model=selected_model,
            params=parsed_params,
        )
    except ModelParamValidationError as exc:
        return templates.TemplateResponse(
            "endpoint_detail.html",
            _endpoint_detail_context(
                request=request,
                db=db,
                session_user=session_user,
                endpoint=endpoint,
                flash_message=str(exc),
            ),
        )

    timeout_value = _parse_bounded_int(timeout_seconds, default=60, min_value=1, max_value=600)
    retry_value = _parse_bounded_int(max_retries, default=1, min_value=0, max_value=10)

    draft_test_result: dict[str, Any] = {
        "provider": normalized_provider,
        "model": selected_model,
        "effective_input": effective_input,
        "provider_response_id": None,
        "result_text": "",
        "usage": None,
        "advisor": None,
        "error": None,
    }
    flash_message = "Draft run completed."
    try:
        creds = resolve_provider_credentials(
            db,
            tenant_id=session_user.tenant_id,
            provider_slug=normalized_provider,
            provider_config_id=selected_provider_config_id,
        )
        draft_test_result["advisor"] = build_token_cost_advisor(
            db=db,
            tenant_id=session_user.tenant_id,
            provider_slug=normalized_provider,
            model=selected_model,
            api_key=creds.api_key,
            api_base=creds.api_base,
            api_version=creds.api_version,
            system_prompt=combined_system_prompt,
            input_payload=effective_input,
            params=validated_params.params,
            metadata=parsed_metadata,
        )
        output_text, provider_response_id, usage = run_provider_completion(
            provider_slug=normalized_provider,
            model=selected_model,
            api_key=creds.api_key,
            api_base=creds.api_base,
            api_version=creds.api_version,
            system_prompt=combined_system_prompt,
            input_payload=effective_input,
            params=validated_params.params,
            timeout_seconds=timeout_value,
            max_retries=retry_value,
        )
        draft_test_result["provider_response_id"] = provider_response_id
        draft_test_result["result_text"] = output_text
        draft_test_result["usage"] = usage
    except Exception as exc:  # noqa: BLE001
        flash_message = f"Draft run failed: {exc}"
        draft_test_result["error"] = str(exc)

    return templates.TemplateResponse(
        "endpoint_detail.html",
        _endpoint_detail_context(
            request=request,
            db=db,
            session_user=session_user,
            endpoint=endpoint,
            flash_message=flash_message,
            draft_test_result=draft_test_result,
        ),
    )


@router.post("/apis/{endpoint_id}/versions")
@router.post("/endpoints/{endpoint_id}/versions")
def endpoint_create_version(
    request: Request,
    endpoint_id: str,
    system_prompt: str = Form(...),
    input_template: str = Form(default=""),
    variable_schema_json: str = Form(default="[]"),
    target_id: str = Form(default=""),
    provider: str = Form(default="openai"),
    provider_config_id: str = Form(default=""),
    model: str = Form(default="gpt-5-nano"),
    model_custom: str = Form(default=""),
    persona_id: str = Form(default=""),
    context_block_ids: list[str] = Form(default=[]),
    timeout_seconds: str = Form(default="60"),
    max_retries: str = Form(default="1"),
    few_shot_enabled: str | None = Form(default=None),
    few_shot_limit: str = Form(default="3"),
    cache_enabled: str | None = Form(default=None),
    cache_ttl_seconds: str = Form(default="300"),
    blocked_input_phrases: str = Form(default=""),
    blocked_output_phrases: str = Form(default=""),
    params_json: str = Form(default="{}"),
    auto_activate: str | None = Form(default=None),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == session_user.tenant_id))
    if endpoint is None:
        return RedirectResponse(_apis_index_path(), status_code=303)

    ready_provider_catalog = list_ready_provider_catalog_for_tenant(db, session_user.tenant_id)
    ready_provider_slugs = {item.slug for item in ready_provider_catalog}
    selected_target_id = target_id.strip() or None
    normalized_provider = ""
    selected_model = ""
    selected_provider_config_id: str | None = None
    if selected_target_id:
        selected_target = get_target(db, session_user.tenant_id, selected_target_id)
        if selected_target is None:
            request.session["endpoint_flash"] = "Selected deployment was not found."
            return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)
        if not selected_target.is_active:
            request.session["endpoint_flash"] = (
                f"Deployment '{selected_target.name}' is disabled. Enable it or choose another deployment."
            )
            return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)
        if selected_target.provider_slug not in ready_provider_slugs:
            request.session["endpoint_flash"] = (
                f"Deployment '{selected_target.name}' uses provider '{selected_target.provider_slug}', which is not ready."
            )
            return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)
        if selected_target.provider_config_id is not None:
            selected_connection = get_tenant_provider_config_by_id(
                db,
                session_user.tenant_id,
                selected_target.provider_config_id,
            )
            if selected_connection is None or not provider_config_is_ready(selected_connection):
                request.session["endpoint_flash"] = (
                    f"Deployment '{selected_target.name}' connection is not ready. Fix provider connection first."
                )
                return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)
        normalized_provider = selected_target.provider_slug
        selected_model = selected_target.model_identifier
    else:
        normalized_provider = ensure_supported_provider_slug(provider)
        selected_model = _resolve_model_identifier(model, model_custom) or "gpt-5-nano"
        if normalized_provider not in ready_provider_slugs:
            request.session["endpoint_flash"] = (
                f"Provider '{normalized_provider}' is not ready. Configure it in Providers first."
            )
            return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)
        selected_provider_config_id = provider_config_id.strip() or None
        if selected_provider_config_id is not None:
            selected_connection = get_tenant_provider_config_by_id(
                db,
                session_user.tenant_id,
                selected_provider_config_id,
            )
            if selected_connection is None or not provider_config_is_ready(selected_connection):
                request.session["endpoint_flash"] = "Selected provider connection is not ready."
                return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)
            if selected_connection.provider_slug != normalized_provider:
                request.session["endpoint_flash"] = "Selected provider connection does not match provider."
                return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)
    structured_params: dict[str, Any] = {
        "timeout_seconds": _parse_bounded_int(timeout_seconds, default=60, min_value=1, max_value=600),
        "max_retries": _parse_bounded_int(max_retries, default=1, min_value=0, max_value=10),
        "few_shot_enabled": few_shot_enabled == "on",
        "few_shot_limit": _parse_bounded_int(few_shot_limit, default=3, min_value=0, max_value=20),
    }

    if cache_enabled == "on":
        structured_params["cache_ttl_seconds"] = _parse_bounded_int(
            cache_ttl_seconds,
            default=300,
            min_value=1,
            max_value=604800,
        )
    else:
        structured_params["cache_ttl_seconds"] = 0

    blocked_input = _parse_csv_list(blocked_input_phrases)
    blocked_output = _parse_csv_list(blocked_output_phrases)
    if blocked_input:
        structured_params["blocked_input_phrases"] = blocked_input
    if blocked_output:
        structured_params["blocked_output_phrases"] = blocked_output

    parsed_params = _parse_json(params_json, {})
    if not isinstance(parsed_params, dict):
        parsed_params = {}
    merged_params = {**structured_params, **parsed_params}
    if selected_target_id:
        merged_params.pop("provider_config_id", None)
    elif selected_provider_config_id:
        merged_params["provider_config_id"] = selected_provider_config_id
    else:
        merged_params.pop("provider_config_id", None)
    for key in ("enable_fallbacks", "routing_strategy", "fallback_targets", "fallback_models", "max_route_attempts"):
        merged_params.pop(key, None)
    try:
        validated_params = validate_model_params(
            provider_slug=normalized_provider,
            model=selected_model,
            params=merged_params,
        )
    except ModelParamValidationError as exc:
        request.session["endpoint_flash"] = str(exc)
        return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)

    parsed_variable_schema = _parse_json(variable_schema_json, [])
    if not isinstance(parsed_variable_schema, list):
        parsed_variable_schema = []

    version_payload = EndpointVersionCreate(
        system_prompt=system_prompt,
        input_template=input_template.strip() or None,
        variable_schema_json=parsed_variable_schema,
        target_id=selected_target_id,
        provider=normalized_provider,
        model=selected_model,
        params_json=validated_params.params,
        persona_id=persona_id.strip() or None,
        context_block_ids=context_block_ids,
    )

    try:
        version = create_endpoint_version_record(
            db,
            endpoint=endpoint,
            tenant_id=session_user.tenant_id,
            created_by_user_id=session_user.user_id,
            payload=version_payload,
        )
    except ValueError as exc:
        request.session["endpoint_flash"] = str(exc)
        return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)

    if endpoint.active_version_id is None or auto_activate == "on":
        endpoint.active_version_id = version.id
        db.add(endpoint)
        db.commit()

    request.session["endpoint_flash"] = f"Version v{version.version} created."
    return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)


@router.post("/apis/{endpoint_id}/activate")
@router.post("/endpoints/{endpoint_id}/activate")
def endpoint_activate_version(
    endpoint_id: str,
    version_id: str = Form(...),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == session_user.tenant_id))
    version = db.scalar(select(EndpointVersion).where(EndpointVersion.id == version_id, EndpointVersion.endpoint_id == endpoint_id))
    if endpoint and version:
        endpoint.active_version_id = version.id
        db.add(endpoint)
        db.commit()

    return RedirectResponse(_api_detail_path(endpoint_id), status_code=303)


@router.post("/apis/{endpoint_id}/test")
@router.post("/endpoints/{endpoint_id}/test")
def endpoint_test_run(
    endpoint_id: str,
    input_text: str = Form(default=""),
    metadata_json: str = Form(default="{}"),
    save_default: str | None = Form(default=None),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == session_user.tenant_id))
    if endpoint is None:
        return RedirectResponse(_apis_index_path(), status_code=303)

    active_version = get_active_version(db, endpoint)
    if active_version is None:
        return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)

    parsed_metadata = _parse_json(metadata_json, {})
    if not isinstance(parsed_metadata, dict):
        parsed_metadata = {}

    payload = JobCreateRequest(
        input=input_text or "Test prompt",
        metadata=parsed_metadata,
        save_default=save_default == "on",
    )
    job = create_job(
        db,
        tenant_id=session_user.tenant_id,
        endpoint=endpoint,
        active_version=active_version,
        request_payload=payload,
    )

    queue = get_queue()
    settings = get_settings()
    queue.enqueue("app.tasks.process_job", job.id, job_id=job.id, job_timeout=settings.job_timeout_seconds)

    return RedirectResponse(f"/runs/{job.id}", status_code=303)


@router.get("/personas", response_class=HTMLResponse)
def personas_page(
    request: Request,
    persona_id: str | None = None,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    personas = list_personas(db, session_user.tenant_id)
    selected = next((persona for persona in personas if persona.id == persona_id), None)

    return templates.TemplateResponse(
        "personas.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="personas",
            personas=personas,
            selected=selected,
        ),
    )


@router.post("/personas/create")
def personas_create(
    name: str = Form(...),
    description: str = Form(default=""),
    instructions: str = Form(...),
    style_json: str = Form(default="{}"),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/personas", status_code=303)

    parsed_style_json = _parse_json(style_json, {})
    if not isinstance(parsed_style_json, dict):
        parsed_style_json = {}

    existing = db.scalar(select(Persona).where(Persona.tenant_id == session_user.tenant_id, Persona.name == name.strip()))
    if existing is None:
        db.add(
            Persona(
                tenant_id=session_user.tenant_id,
                name=name.strip(),
                description=description.strip() or None,
                instructions=instructions,
                style_json=parsed_style_json,
            )
        )
        db.commit()

    return RedirectResponse("/personas", status_code=303)


@router.post("/personas/{persona_id}/update")
def personas_update(
    persona_id: str,
    name: str = Form(...),
    description: str = Form(default=""),
    instructions: str = Form(...),
    style_json: str = Form(default="{}"),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/personas", status_code=303)

    persona = db.scalar(select(Persona).where(Persona.id == persona_id, Persona.tenant_id == session_user.tenant_id))
    if persona:
        parsed_style_json = _parse_json(style_json, {})
        if not isinstance(parsed_style_json, dict):
            parsed_style_json = {}
        persona.name = name.strip()
        persona.description = description.strip() or None
        persona.instructions = instructions
        persona.style_json = parsed_style_json
        db.add(persona)
        db.commit()

    return RedirectResponse(f"/personas?persona_id={persona_id}", status_code=303)


@router.post("/personas/{persona_id}/delete")
def personas_delete(
    persona_id: str,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/personas", status_code=303)

    persona = db.scalar(select(Persona).where(Persona.id == persona_id, Persona.tenant_id == session_user.tenant_id))
    if persona:
        in_use = db.scalar(select(func.count(EndpointVersion.id)).where(EndpointVersion.persona_id == persona.id)) or 0
        if in_use == 0:
            db.delete(persona)
            db.commit()

    return RedirectResponse("/personas", status_code=303)


@router.get("/contexts", response_class=HTMLResponse)
def contexts_page(
    request: Request,
    context_id: str | None = None,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    contexts = list_context_blocks(db, session_user.tenant_id)
    selected = next((block for block in contexts if block.id == context_id), None)

    return templates.TemplateResponse(
        "contexts.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="contexts",
            contexts=contexts,
            selected=selected,
        ),
    )


@router.post("/contexts/create")
def contexts_create(
    name: str = Form(...),
    content: str = Form(...),
    tags: str = Form(default=""),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/contexts", status_code=303)

    existing = db.scalar(select(ContextBlock).where(ContextBlock.tenant_id == session_user.tenant_id, ContextBlock.name == name.strip()))
    if existing is None:
        db.add(
            ContextBlock(
                tenant_id=session_user.tenant_id,
                name=name.strip(),
                content=content,
                tags=[item.strip() for item in tags.split(",") if item.strip()],
            )
        )
        db.commit()

    return RedirectResponse("/contexts", status_code=303)


@router.post("/contexts/{context_id}/update")
def contexts_update(
    context_id: str,
    name: str = Form(...),
    content: str = Form(...),
    tags: str = Form(default=""),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/contexts", status_code=303)

    block = db.scalar(select(ContextBlock).where(ContextBlock.id == context_id, ContextBlock.tenant_id == session_user.tenant_id))
    if block:
        block.name = name.strip()
        block.content = content
        block.tags = [item.strip() for item in tags.split(",") if item.strip()]
        db.add(block)
        db.commit()

    return RedirectResponse(f"/contexts?context_id={context_id}", status_code=303)


@router.post("/contexts/{context_id}/delete")
def contexts_delete(
    context_id: str,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/contexts", status_code=303)

    block = db.scalar(select(ContextBlock).where(ContextBlock.id == context_id, ContextBlock.tenant_id == session_user.tenant_id))
    if block:
        db.execute(EndpointVersionContext.__table__.delete().where(EndpointVersionContext.context_block_id == block.id))
        db.delete(block)
        db.commit()

    return RedirectResponse("/contexts", status_code=303)


@router.get("/api-keys", response_class=HTMLResponse)
def api_keys_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    keys = db.scalars(select(ApiKey).where(ApiKey.tenant_id == session_user.tenant_id).order_by(ApiKey.created_at.desc())).all()
    endpoints = db.scalars(select(Endpoint).where(Endpoint.tenant_id == session_user.tenant_id).order_by(Endpoint.name.asc())).all()

    flash_key = request.session.pop("new_api_key", None)
    flash_message = request.session.pop("api_key_flash", None)

    return templates.TemplateResponse(
        "api_keys.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="api-keys",
            keys=keys,
            endpoints=endpoints,
            new_api_key=flash_key,
            flash_message=flash_message,
        ),
    )


@router.post("/api-keys/create")
def api_keys_create(
    request: Request,
    name: str = Form(...),
    scope_mode: str = Form(default="all"),
    endpoint_ids: list[str] = Form(default=[]),
    rate_limit_per_min: int = Form(default=60),
    monthly_quota: int = Form(default=10000),
    next_url: str = Form(default=""),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    normalized_scope_mode = (scope_mode or "all").strip().lower()
    valid_endpoint_ids = set(
        db.scalars(select(Endpoint.id).where(Endpoint.tenant_id == session_user.tenant_id)).all()
    )
    selected_endpoint_ids = [endpoint_id for endpoint_id in endpoint_ids if endpoint_id in valid_endpoint_ids]

    if normalized_scope_mode != "all" and not selected_endpoint_ids:
        message = "Select at least one endpoint when using selected endpoint scope."
        safe_next = _safe_next_path(next_url) or "/api-keys"
        if safe_next == "/builder":
            request.session["builder_flash"] = message
        else:
            request.session["api_key_flash"] = message
        return RedirectResponse(safe_next, status_code=303)

    scopes = {"all": True} if normalized_scope_mode == "all" else {"all": False, "endpoint_ids": selected_endpoint_ids}
    key, raw = create_virtual_key(
        db,
        session_user.tenant_id,
        ApiKeyCreate(
            name=name,
            scopes=scopes,
            rate_limit_per_min=rate_limit_per_min,
            monthly_quota=monthly_quota,
        ),
    )
    request.session["new_api_key"] = {
        "id": key.id,
        "name": key.name,
        "prefix": key.key_prefix,
        "value": raw,
    }
    return RedirectResponse(_safe_next_path(next_url) or "/api-keys", status_code=303)


@router.post("/api-keys/{key_id}/deactivate")
def api_keys_deactivate(
    key_id: str,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    key = db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == session_user.tenant_id))
    if key:
        key.is_active = False
        db.add(key)
        db.commit()
    return RedirectResponse("/api-keys", status_code=303)


@router.get("/test-lab", response_class=HTMLResponse)
def test_lab_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    endpoints = db.scalars(
        select(Endpoint).where(Endpoint.tenant_id == session_user.tenant_id).order_by(Endpoint.name.asc())
    ).all()
    provider_catalog = list_ready_provider_catalog_for_tenant(db, session_user.tenant_id)
    ready_connections = list_ready_provider_connections_for_tenant(db, session_user.tenant_id)
    provider_models = _provider_models_for_catalog(provider_catalog)
    routes = _default_test_routes(
        ready_connections=ready_connections,
        provider_catalog=provider_catalog,
        compare_pack="budget",
    )
    flash_message = request.session.pop("test_lab_flash", None)

    return templates.TemplateResponse(
        "test_lab.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="test-lab",
            endpoints=endpoints,
            configured_providers=provider_catalog,
            ready_connections=ready_connections,
            provider_models_json=json.dumps(provider_models),
            compare_packs=_compare_pack_catalog(),
            selected_compare_pack="budget",
            routes_json=json.dumps(routes),
            selected_endpoint_id="",
            input_text="",
            metadata_json_text="{}",
            effective_input_text="",
            results=[],
            run_error=None,
            flash_message=flash_message,
        ),
    )


@router.post("/test-lab/run", response_class=HTMLResponse)
def test_lab_run(
    request: Request,
    endpoint_id: str = Form(default=""),
    input_text: str = Form(default=""),
    metadata_json: str = Form(default="{}"),
    routes_json: str = Form(default="[]"),
    compare_pack: str = Form(default="custom"),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    endpoints = db.scalars(
        select(Endpoint).where(Endpoint.tenant_id == session_user.tenant_id).order_by(Endpoint.name.asc())
    ).all()
    provider_catalog = list_ready_provider_catalog_for_tenant(db, session_user.tenant_id)
    ready_connections = list_ready_provider_connections_for_tenant(db, session_user.tenant_id)
    provider_models = _provider_models_for_catalog(provider_catalog)
    run_error: str | None = None

    parsed_metadata = _parse_json(metadata_json, {})
    if not isinstance(parsed_metadata, dict):
        parsed_metadata = {}

    selected_compare_pack = (compare_pack or "custom").strip().lower()
    routes, route_errors = _parse_test_routes_json(
        raw=routes_json,
        ready_connections=ready_connections,
        provider_models=provider_models,
    )
    if route_errors:
        run_error = " ".join(route_errors)
    if not routes and selected_compare_pack != "custom":
        routes = _default_test_routes(
            ready_connections=ready_connections,
            provider_catalog=provider_catalog,
            compare_pack=selected_compare_pack,
        )
    if not routes:
        run_error = run_error or "Add at least one valid comparison route."

    system_prompt = ""
    params: dict[str, Any] = {}
    payload_input = input_text.strip() or "Test prompt"
    selected_endpoint_id = endpoint_id.strip()
    endpoint_active_version: EndpointVersion | None = None
    if selected_endpoint_id:
        endpoint = db.scalar(
            select(Endpoint).where(
                Endpoint.id == selected_endpoint_id,
                Endpoint.tenant_id == session_user.tenant_id,
            )
        )
        if endpoint is None:
            run_error = "Selected API was not found."
        else:
            endpoint_active_version = get_active_version(db, endpoint)
            if endpoint_active_version is None:
                run_error = "Selected API has no live version."
            else:
                tenant_variables = tenant_variables_map(db, session_user.tenant_id)
                rendered_input, _ = render_job_input(
                    input_template=endpoint_active_version.input_template,
                    input_text=payload_input,
                    metadata=parsed_metadata,
                    tenant_variables=tenant_variables,
                )
                payload_input = rendered_input or payload_input

                persona = None
                if endpoint_active_version.persona_id:
                    persona = db.scalar(
                        select(Persona).where(
                            Persona.id == endpoint_active_version.persona_id,
                            Persona.tenant_id == session_user.tenant_id,
                        )
                    )
                context_blocks = list_context_blocks_for_version(db, session_user.tenant_id, endpoint_active_version.id)
                system_prompt = compose_system_prompt(
                    system_prompt=endpoint_active_version.system_prompt,
                    persona=persona,
                    context_blocks=context_blocks,
                )
                params = endpoint_active_version.params_json or {}

    results: list[dict[str, Any]] = []
    if run_error is None:
        for route in routes:
            provider_slug = route["provider_slug"]
            model = route["model"]
            provider_config_id = route["provider_config_id"]
            started = time.perf_counter()
            provider_item = get_provider_catalog_item(provider_slug)
            try:
                creds = resolve_provider_credentials(
                    db,
                    tenant_id=session_user.tenant_id,
                    provider_slug=provider_slug,
                    provider_config_id=provider_config_id,
                )
                validated_params = validate_model_params(
                    provider_slug=provider_slug,
                    model=model,
                    params=params,
                ).params
                advisor = build_token_cost_advisor(
                    db=db,
                    tenant_id=session_user.tenant_id,
                    provider_slug=provider_slug,
                    model=model,
                    api_key=creds.api_key,
                    api_base=creds.api_base,
                    api_version=creds.api_version,
                    system_prompt=system_prompt,
                    input_payload=payload_input,
                    params=validated_params,
                    metadata=parsed_metadata,
                )
                output_text, provider_response_id, usage = run_provider_completion(
                    provider_slug=provider_slug,
                    model=model,
                    api_key=creds.api_key,
                    api_base=creds.api_base,
                    api_version=creds.api_version,
                    system_prompt=system_prompt,
                    input_payload=payload_input,
                    params=validated_params,
                    timeout_seconds=90,
                    max_retries=1,
                )
                elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                results.append(
                    {
                        "provider": provider_slug,
                        "model": model,
                        "provider_config_id": provider_config_id,
                        "latency_ms": elapsed_ms,
                        "result_text": output_text,
                        "error": None,
                        "usage": usage,
                        "advisor": advisor,
                        "provider_response_id": provider_response_id,
                        "source_tenant_id": creds.source_tenant_id,
                        "docs_url": provider_item.docs_url if provider_item else None,
                        "realtime_docs_url": provider_item.realtime_docs_url if provider_item else None,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                results.append(
                    {
                        "provider": provider_slug,
                        "model": model,
                        "provider_config_id": provider_config_id,
                        "latency_ms": elapsed_ms,
                        "result_text": None,
                        "error": str(exc),
                        "usage": None,
                        "advisor": None,
                        "provider_response_id": None,
                        "source_tenant_id": None,
                        "docs_url": provider_item.docs_url if provider_item else None,
                        "realtime_docs_url": provider_item.realtime_docs_url if provider_item else None,
                    }
                )

    return templates.TemplateResponse(
        "test_lab.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="test-lab",
            endpoints=endpoints,
            configured_providers=provider_catalog,
            ready_connections=ready_connections,
            provider_models_json=json.dumps(provider_models),
            compare_packs=_compare_pack_catalog(),
            selected_compare_pack=selected_compare_pack,
            routes_json=json.dumps(routes),
            selected_endpoint_id=selected_endpoint_id,
            input_text=input_text,
            metadata_json_text=metadata_json,
            effective_input_text=payload_input if run_error is None else "",
            results=results,
            run_error=run_error,
            flash_message=None,
        ),
    )


@router.post("/test-lab/promote")
def test_lab_promote(
    request: Request,
    endpoint_id: str = Form(...),
    provider_slug: str = Form(...),
    model: str = Form(...),
    provider_config_id: str = Form(default=""),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/test-lab", status_code=303)

    endpoint = db.scalar(
        select(Endpoint).where(
            Endpoint.id == endpoint_id.strip(),
            Endpoint.tenant_id == session_user.tenant_id,
        )
    )
    if endpoint is None:
        request.session["test_lab_flash"] = "Promotion failed: selected API not found."
        return RedirectResponse("/test-lab", status_code=303)

    live_version = get_active_version(db, endpoint)
    if live_version is None:
        request.session["test_lab_flash"] = "Promotion failed: selected API has no live version."
        return RedirectResponse("/test-lab", status_code=303)

    normalized_provider = ensure_supported_provider_slug(provider_slug)
    selected_model = (model or "").strip()
    if not selected_model:
        request.session["test_lab_flash"] = "Promotion failed: model is required."
        return RedirectResponse("/test-lab", status_code=303)

    provider_config_id_value = provider_config_id.strip() or None
    if provider_config_id_value:
        provider_config = get_tenant_provider_config_by_id(
            db,
            session_user.tenant_id,
            provider_config_id_value,
        )
        if provider_config is None:
            request.session["test_lab_flash"] = "Promotion failed: selected connection not found."
            return RedirectResponse("/test-lab", status_code=303)
        if provider_config.provider_slug != normalized_provider:
            request.session["test_lab_flash"] = "Promotion failed: connection/provider mismatch."
            return RedirectResponse("/test-lab", status_code=303)
        if not provider_config_is_ready(provider_config):
            request.session["test_lab_flash"] = "Promotion failed: selected connection is not ready."
            return RedirectResponse("/test-lab", status_code=303)

    target_stmt = select(Target).where(
        Target.tenant_id == session_user.tenant_id,
        Target.provider_slug == normalized_provider,
        Target.model_identifier == selected_model,
    )
    if provider_config_id_value:
        target_stmt = target_stmt.where(Target.provider_config_id == provider_config_id_value)
    else:
        target_stmt = target_stmt.where(Target.provider_config_id.is_(None))
    target = db.scalar(target_stmt.order_by(Target.is_verified.desc(), Target.updated_at.desc()))

    if target is None:
        seed_params = dict(live_version.params_json or {})
        try:
            validated_seed = validate_model_params(
                provider_slug=normalized_provider,
                model=selected_model,
                params=seed_params,
                strict_known_keys=False,
            ).params
        except ModelParamValidationError:
            validated_seed = {}
        target_payload = TargetCreate(
            name=_next_auto_target_name(db, session_user.tenant_id, normalized_provider, selected_model),
            provider_config_id=provider_config_id_value,
            provider_slug=normalized_provider,
            capability_profile="responses_chat",
            model_identifier=selected_model,
            params_json=validated_seed,
            is_active=True,
        )
        target = create_target_record(db, session_user.tenant_id, target_payload)
        verify_target(db, target)

    context_ids = db.scalars(
        select(EndpointVersionContext.context_block_id).where(
            EndpointVersionContext.endpoint_version_id == live_version.id
        )
    ).all()
    version_payload = EndpointVersionCreate(
        system_prompt=live_version.system_prompt,
        input_template=live_version.input_template,
        variable_schema_json=list(live_version.variable_schema_json or []),
        target_id=target.id,
        provider=normalized_provider,
        model=selected_model,
        params_json=dict(live_version.params_json or {}),
        persona_id=live_version.persona_id,
        context_block_ids=list(context_ids),
    )
    try:
        new_version = create_endpoint_version_record(
            db,
            endpoint=endpoint,
            tenant_id=session_user.tenant_id,
            created_by_user_id=session_user.user_id,
            payload=version_payload,
        )
    except ValueError as exc:
        request.session["test_lab_flash"] = f"Promotion failed: {exc}"
        return RedirectResponse("/test-lab", status_code=303)

    endpoint.active_version_id = new_version.id
    db.add(endpoint)
    db.commit()
    request.session["test_lab_flash"] = (
        f"Promoted {normalized_provider}/{selected_model} to API '{endpoint.name}' as v{new_version.version} and activated."
    )
    return RedirectResponse(_api_detail_path(endpoint.id), status_code=303)


@router.get("/batches", response_class=HTMLResponse)
def batches_page(
    request: Request,
    batch_id: str | None = None,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/dashboard", status_code=303)

    endpoints = db.scalars(
        select(Endpoint).where(Endpoint.tenant_id == session_user.tenant_id).order_by(Endpoint.name.asc())
    ).all()
    batches = list_provider_batches_for_tenant(db, session_user.tenant_id, limit=120)

    queue = get_queue()
    settings = get_settings()
    now = datetime.now(UTC)
    enqueued_poll = False
    for run in batches:
        if run.status in {"completed", "failed", "canceled"}:
            continue
        if run.next_poll_at is not None and run.next_poll_at > now:
            continue
        queue.enqueue("app.tasks.poll_provider_batch_run", run.id, 0, job_timeout=settings.job_timeout_seconds)
        run.next_poll_at = now + timedelta(seconds=settings.provider_batch_poll_interval_seconds)
        db.add(run)
        enqueued_poll = True
    if enqueued_poll:
        db.commit()

    selected_batch_id = (batch_id or "").strip()
    selected_jobs: list[Job] = []
    if selected_batch_id:
        selected_jobs = list_jobs_for_provider_batch(
            db,
            session_user.tenant_id,
            selected_batch_id,
            limit=600,
        )

    flash_message = request.session.pop("batch_flash", None)

    return templates.TemplateResponse(
        "batches.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="batches",
            endpoints=endpoints,
            batches=batches,
            selected_batch_id=selected_batch_id,
            selected_jobs=selected_jobs,
            selected_run=next((run for run in batches if run.id == selected_batch_id), None),
            flash_message=flash_message,
        ),
    )


@router.post("/batches/create")
def batches_create(
    request: Request,
    endpoint_id: str = Form(...),
    batch_name: str = Form(default=""),
    inputs_text: str = Form(default=""),
    metadata_json: str = Form(default="{}"),
    subtenant_code: str = Form(default=""),
    service_tier: str = Form(default="auto"),
    save_default: str | None = Form(default=None),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/batches", status_code=303)

    endpoint = db.scalar(
        select(Endpoint).where(Endpoint.id == endpoint_id.strip(), Endpoint.tenant_id == session_user.tenant_id)
    )
    if endpoint is None:
        request.session["batch_flash"] = "Endpoint not found."
        return RedirectResponse("/batches", status_code=303)

    active_version = get_active_version(db, endpoint)
    if active_version is None:
        request.session["batch_flash"] = "Endpoint has no live version."
        return RedirectResponse("/batches", status_code=303)

    lines = [line.strip() for line in (inputs_text or "").splitlines() if line.strip()]
    if not lines:
        request.session["batch_flash"] = "Add at least one input line for the batch."
        return RedirectResponse("/batches", status_code=303)
    if len(lines) > 1000:
        request.session["batch_flash"] = "Batch too large in one submit. Limit is 1000 lines."
        return RedirectResponse("/batches", status_code=303)

    parsed_metadata = _parse_json(metadata_json, {})
    if not isinstance(parsed_metadata, dict):
        parsed_metadata = {}
    normalized_tier = service_tier.strip().lower()
    if normalized_tier not in {"auto", "default", "flex", "priority"}:
        normalized_tier = "auto"
    parsed_metadata["service_tier"] = normalized_tier

    items = [
        ProviderBatchItemRequest(
            input=line,
            metadata={},
            subtenant_code=subtenant_code.strip() or None,
            save_default=save_default == "on",
        )
        for line in lines
    ]

    payload = ProviderBatchCreateRequest(
        items=items,
        batch_name=(batch_name or "").strip() or None,
        metadata=parsed_metadata,
        subtenant_code=subtenant_code.strip() or None,
        save_default=save_default == "on",
        service_tier=normalized_tier,
        completion_window="24h",
    )
    try:
        run = create_provider_batch_run(
            db,
            tenant_id=session_user.tenant_id,
            endpoint=endpoint,
            active_version=active_version,
            payload=payload,
            request_api_key_id=None,
            created_by_user_id=session_user.user_id,
        )
    except RuntimeError as exc:
        request.session["batch_flash"] = str(exc)
        return RedirectResponse("/batches", status_code=303)

    queue = get_queue()
    settings = get_settings()
    queue.enqueue("app.tasks.submit_provider_batch_run", run.id, job_timeout=max(600, settings.job_timeout_seconds))

    batch_name_value = (batch_name or "").strip() or run.id
    request.session["batch_flash"] = (
        f"Provider batch '{batch_name_value}' submitted with {len(lines)} jobs."
    )
    return RedirectResponse(f"/batches?batch_id={run.id}", status_code=303)


@router.post("/batches/{batch_id}/cancel")
def batches_cancel(
    batch_id: str,
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/batches", status_code=303)

    run = get_provider_batch_for_tenant(db, session_user.tenant_id, batch_id)
    if run is None:
        request.session["batch_flash"] = "Batch not found."
        return RedirectResponse("/batches", status_code=303)

    updated = request_cancel_provider_batch_run(db, run)
    if updated.provider_batch_id and updated.status not in {"completed", "failed", "canceled"}:
        queue = get_queue()
        settings = get_settings()
        queue.enqueue("app.tasks.poll_provider_batch_run", updated.id, 0, job_timeout=settings.job_timeout_seconds)
    request.session["batch_flash"] = f"Batch '{updated.id}' set to {updated.status}."
    return RedirectResponse(f"/batches?batch_id={updated.id}", status_code=303)


@router.get("/runs", response_class=HTMLResponse)
@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    endpoint_id: str | None = None,
    status_filter: str | None = None,
    provider_filter: str | None = None,
    subtenant_code: str | None = None,
    cache_filter: str | None = None,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    filters = [Job.tenant_id == session_user.tenant_id]
    if endpoint_id:
        filters.append(Job.endpoint_id == endpoint_id)
    if status_filter:
        try:
            filters.append(Job.status == JobStatus(status_filter))
        except ValueError:
            pass
    if provider_filter:
        filters.append(Job.provider_used == provider_filter)
    if subtenant_code:
        filters.append(Job.subtenant_code == subtenant_code.strip())
    if cache_filter == "hit":
        filters.append(Job.cache_hit.is_(True))
    if cache_filter == "miss":
        filters.append(Job.cache_hit.is_(False))

    jobs = db.scalars(select(Job).where(and_(*filters)).order_by(Job.created_at.desc()).limit(200)).all()
    endpoints = db.scalars(select(Endpoint).where(Endpoint.tenant_id == session_user.tenant_id)).all()
    providers = sorted({job.provider_used for job in jobs if job.provider_used})

    return templates.TemplateResponse(
        "jobs.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="runs",
            jobs=jobs,
            endpoints=endpoints,
            endpoint_id=endpoint_id,
            status_filter=status_filter,
            provider_filter=provider_filter,
            subtenant_code=subtenant_code,
            providers=providers,
            cache_filter=cache_filter,
        ),
    )


@router.get("/runs/{job_id}", response_class=HTMLResponse)
@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    job = db.scalar(select(Job).where(Job.id == job_id, Job.tenant_id == session_user.tenant_id))
    if job is None:
        return RedirectResponse("/runs", status_code=303)

    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == job.endpoint_id, Endpoint.tenant_id == session_user.tenant_id))
    version = db.scalar(select(EndpointVersion).where(EndpointVersion.id == job.endpoint_version_id))
    usage_payload = job.usage_json if isinstance(job.usage_json, dict) else {}
    advisor = usage_payload.get("advisor") if isinstance(usage_payload.get("advisor"), dict) else None

    queue_wait_ms: float | None = None
    runtime_ms: float | None = None
    if job.started_at is not None:
        queue_wait_ms = max((job.started_at - job.created_at).total_seconds() * 1000.0, 0.0)
    if job.started_at is not None and job.finished_at is not None:
        runtime_ms = max((job.finished_at - job.started_at).total_seconds() * 1000.0, 0.0)

    timeline_events: list[dict[str, str]] = [
        {
            "title": "Received",
            "at": str(job.created_at),
            "detail": "Run accepted and queued.",
        }
    ]
    if job.started_at is not None:
        wait_text = f"Queue wait {queue_wait_ms:.0f} ms." if queue_wait_ms is not None else "Run started."
        timeline_events.append(
            {
                "title": "Started",
                "at": str(job.started_at),
                "detail": wait_text,
            }
        )
    if job.cache_hit:
        source = job.cached_from_job_id or "-"
        timeline_events.append(
            {
                "title": "Cache Reused",
                "at": str(job.finished_at or job.started_at or job.created_at),
                "detail": f"Result served from cache (source run {source}).",
            }
        )
    if job.provider_used or job.model_used:
        timeline_events.append(
            {
                "title": "Provider Route",
                "at": str(job.started_at or job.created_at),
                "detail": f"Selected {job.provider_used or '-'} / {job.model_used or '-'}",
            }
        )
    if job.finished_at is not None:
        runtime_text = f"Runtime {runtime_ms:.0f} ms." if runtime_ms is not None else "Run completed."
        timeline_events.append(
            {
                "title": "Finished",
                "at": str(job.finished_at),
                "detail": f"Status: {job.status.value}. {runtime_text}",
            }
        )
    if job.estimated_cost_usd is not None:
        timeline_events.append(
            {
                "title": "Cost Settled",
                "at": str(job.finished_at or job.created_at),
                "detail": f"Estimated cost recorded: ${job.estimated_cost_usd:.8f}.",
            }
        )

    return templates.TemplateResponse(
        "job_detail.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="runs",
            job=job,
            endpoint=endpoint,
            version=version,
            advisor=advisor,
            queue_wait_ms=queue_wait_ms,
            runtime_ms=runtime_ms,
            timeline_events=timeline_events,
        ),
    )


@router.post("/runs/{job_id}/save")
@router.post("/jobs/{job_id}/save")
def job_save_training(
    job_id: str,
    feedback: str = Form(default=""),
    edited_ideal_output: str = Form(default=""),
    tags: str = Form(default=""),
    save_mode: str = Form(default="full"),
    is_few_shot: str | None = Form(default=None),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    job = get_job_for_tenant(db, session_user.tenant_id, job_id)
    if job is None:
        return RedirectResponse(f"/runs/{job_id}", status_code=303)

    payload = SaveTrainingRequest(
        feedback=feedback or None,
        edited_ideal_output=edited_ideal_output or None,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        save_mode="redacted" if save_mode == "redacted" else "full",
        is_few_shot=is_few_shot == "on",
    )
    create_training_event_from_job(db, tenant_id=session_user.tenant_id, job=job, payload=payload)
    return RedirectResponse(f"/runs/{job_id}", status_code=303)


@router.get("/training", response_class=HTMLResponse)
def training_page(
    request: Request,
    endpoint_id: str | None = None,
    feedback: str | None = None,
    subtenant_code: str | None = None,
    few_shot_filter: str | None = None,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    filters = [TrainingEvent.tenant_id == session_user.tenant_id]
    if endpoint_id:
        filters.append(TrainingEvent.endpoint_id == endpoint_id)
    if feedback:
        filters.append(TrainingEvent.feedback == feedback)
    if subtenant_code:
        filters.append(TrainingEvent.subtenant_code == subtenant_code.strip())
    if few_shot_filter == "selected":
        filters.append(TrainingEvent.is_few_shot.is_(True))
    elif few_shot_filter == "unselected":
        filters.append(TrainingEvent.is_few_shot.is_(False))

    events = db.scalars(select(TrainingEvent).where(and_(*filters)).order_by(TrainingEvent.created_at.desc()).limit(200)).all()
    endpoints = db.scalars(select(Endpoint).where(Endpoint.tenant_id == session_user.tenant_id)).all()
    flash_message = request.session.pop("training_flash", None)

    return templates.TemplateResponse(
        "training.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="training",
            events=events,
            endpoints=endpoints,
            endpoint_id=endpoint_id,
            feedback=feedback,
            subtenant_code=subtenant_code,
            few_shot_filter=few_shot_filter or "all",
            flash_message=flash_message,
        ),
    )


@router.post("/training/{event_id}/few-shot")
def training_toggle_few_shot(
    event_id: str,
    enabled: str = Form(default="0"),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/training", status_code=303)

    event = db.scalar(
        select(TrainingEvent).where(
            TrainingEvent.id == event_id,
            TrainingEvent.tenant_id == session_user.tenant_id,
        )
    )
    if event is None:
        return RedirectResponse("/training", status_code=303)

    event.is_few_shot = enabled == "1"
    db.add(event)
    db.commit()
    return RedirectResponse("/training", status_code=303)


@router.post("/training/few-shot/create")
def training_create_manual_few_shot(
    request: Request,
    endpoint_id: str = Form(...),
    input_text: str = Form(default=""),
    output_text: str = Form(default=""),
    tags: str = Form(default=""),
    feedback: str = Form(default=""),
    subtenant_code: str = Form(default=""),
    save_mode: str = Form(default="full"),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/training", status_code=303)

    endpoint = db.scalar(
        select(Endpoint).where(
            Endpoint.id == endpoint_id,
            Endpoint.tenant_id == session_user.tenant_id,
        )
    )
    if endpoint is None:
        request.session["training_flash"] = "Endpoint not found."
        return RedirectResponse("/training", status_code=303)
    if endpoint.active_version_id is None:
        request.session["training_flash"] = "Endpoint has no active version."
        return RedirectResponse("/training", status_code=303)

    cleaned_input = input_text.strip()
    cleaned_output = output_text.strip()
    if not cleaned_input or not cleaned_output:
        request.session["training_flash"] = "Few-shot input and output are required."
        return RedirectResponse("/training", status_code=303)

    mode = SaveMode.redacted if save_mode == "redacted" else SaveMode.full
    manual_tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
    if "manual_few_shot" not in manual_tags:
        manual_tags.append("manual_few_shot")

    event = TrainingEvent(
        tenant_id=session_user.tenant_id,
        endpoint_id=endpoint.id,
        endpoint_version_id=endpoint.active_version_id,
        subtenant_code=subtenant_code.strip() or None,
        job_id=None,
        input_json={"input": cleaned_input, "messages": None, "metadata": {"source": "manual_few_shot"}},
        output_text=cleaned_output,
        feedback=feedback.strip() or None,
        edited_ideal_output=None,
        tags=manual_tags,
        is_few_shot=True,
        save_mode=mode,
        redacted_input_json={"redacted": True} if mode == SaveMode.redacted else None,
        redacted_output_text="[REDACTED]" if mode == SaveMode.redacted else None,
    )
    db.add(event)
    db.commit()
    request.session["training_flash"] = "Manual few-shot example saved."
    return RedirectResponse("/training", status_code=303)


@router.post("/training/export")
def training_export_form(
    endpoint_id: str = Form(default=""),
    feedback: str = Form(default=""),
    subtenant_code: str = Form(default=""),
    tags: str = Form(default=""),
    few_shot_filter: str = Form(default="all"),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect

    payload = TrainingExportRequest(
        endpoint_id=endpoint_id or None,
        feedback=feedback or None,
        subtenant_code=subtenant_code.strip() or None,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        few_shot_only=True if few_shot_filter == "selected" else False if few_shot_filter == "unselected" else None,
    )
    events = query_training_events(db, session_user.tenant_id, payload)
    filename = f"training_export_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.jsonl"

    return StreamingResponse(
        export_training_jsonl(events),
        media_type="application/jsonl",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/portal", response_class=HTMLResponse)
def portal_links_page(
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/dashboard", status_code=303)

    links = list_portal_links(db, session_user.tenant_id)
    flash_message = request.session.pop("portal_flash", None)
    new_access = request.session.pop("portal_new_access", None)
    return templates.TemplateResponse(
        "portal_links.html",
        _base_context(
            request=request,
            db=db,
            session_user=session_user,
            active_nav="portal",
            links=links,
            flash_message=flash_message,
            new_access=new_access,
        ),
    )


@router.post("/portal/links/create")
def portal_links_create(
    request: Request,
    subtenant_code: str = Form(...),
    expires_in_days: str = Form(default="7"),
    permission_view_jobs: str | None = Form(default=None),
    permission_add_feedback: str | None = Form(default=None),
    permission_edit_ideal_output: str | None = Form(default=None),
    permission_export_training: str | None = Form(default=None),
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin", "dev"}:
        return RedirectResponse("/portal", status_code=303)

    cleaned_subtenant_code = subtenant_code.strip()
    if not cleaned_subtenant_code:
        request.session["portal_flash"] = "Sub-tenant code is required."
        return RedirectResponse("/portal", status_code=303)

    parsed_days = _parse_bounded_int(expires_in_days, default=7, min_value=1, max_value=365)
    permissions: list[str] = []
    if permission_view_jobs == "on":
        permissions.append(PERMISSION_VIEW_JOBS)
    if permission_add_feedback == "on":
        permissions.append(PERMISSION_ADD_FEEDBACK)
    if permission_edit_ideal_output == "on":
        permissions.append(PERMISSION_EDIT_IDEAL_OUTPUT)
    if permission_export_training == "on":
        permissions.append(PERMISSION_EXPORT_TRAINING)

    payload = PortalLinkCreate(
        subtenant_code=cleaned_subtenant_code,
        expires_at=datetime.now(UTC) + timedelta(days=parsed_days),
        permissions=permissions,
    )
    link, raw_token = create_portal_link(
        db,
        tenant_id=session_user.tenant_id,
        created_by_user_id=session_user.user_id,
        payload=payload,
    )
    request.session["portal_flash"] = f"Portal link created for '{link.subtenant_code}'. Share it securely."
    request.session["portal_new_access"] = {
        "url": f"/portal/access/{raw_token}",
        "subtenant_code": link.subtenant_code,
        "expires_at": link.expires_at.isoformat(),
    }
    return RedirectResponse("/portal", status_code=303)


@router.post("/portal/links/{link_id}/revoke")
def portal_links_revoke(
    link_id: str,
    request: Request,
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
):
    redirect = _ensure_user(session_user)
    if redirect:
        return redirect
    if session_user.role not in {"owner", "admin"}:
        return RedirectResponse("/portal", status_code=303)

    link = get_portal_link(db, session_user.tenant_id, link_id)
    if link is None:
        request.session["portal_flash"] = "Portal link not found."
        return RedirectResponse("/portal", status_code=303)

    revoke_portal_link(db, link)
    request.session["portal_flash"] = f"Portal link for '{link.subtenant_code}' revoked."
    return RedirectResponse("/portal", status_code=303)


@router.get("/portal/access/{token}", response_class=HTMLResponse)
def portal_access(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    result = resolve_portal_token(db, token)
    if not result.ok or result.link is None:
        return templates.TemplateResponse(
            "portal_access_error.html",
            {
                "request": request,
                "message": result.reason,
            },
        )

    link = result.link
    permissions = sorted(link_permissions(link))
    request.session["portal_link_id"] = link.id
    request.session["portal_tenant_id"] = link.tenant_id
    request.session["portal_subtenant_code"] = link.subtenant_code
    request.session["portal_permissions"] = permissions
    request.session["portal_expires_at_ts"] = int(link.expires_at.timestamp())
    return RedirectResponse("/portal/review/jobs", status_code=303)


@router.get("/portal/session-expired", response_class=HTMLResponse)
def portal_session_expired(request: Request):
    return templates.TemplateResponse(
        "portal_session_expired.html",
        {"request": request},
    )


@router.post("/portal/review/logout")
def portal_review_logout(request: Request):
    _clear_portal_session(request)
    return RedirectResponse("/portal/session-expired", status_code=303)


@router.get("/portal/review/jobs", response_class=HTMLResponse)
def portal_review_jobs(
    request: Request,
    status_filter: str | None = None,
    db: Session = Depends(get_db),
):
    portal = _ensure_portal_permission(request, db, PERMISSION_VIEW_JOBS)
    if isinstance(portal, RedirectResponse):
        return portal

    filters = [
        Job.tenant_id == portal["tenant_id"],
        Job.subtenant_code == portal["subtenant_code"],
    ]
    if status_filter:
        try:
            filters.append(Job.status == JobStatus(status_filter))
        except ValueError:
            pass
    jobs = db.scalars(select(Job).where(and_(*filters)).order_by(Job.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse(
        "portal_review_jobs.html",
        {
            "request": request,
            "portal": portal,
            "jobs": jobs,
            "status_filter": status_filter,
            "can_export": PERMISSION_EXPORT_TRAINING in set(portal["permissions"]),
        },
    )


@router.get("/portal/review/jobs/{job_id}", response_class=HTMLResponse)
def portal_review_job_detail(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    portal = _ensure_portal_permission(request, db, PERMISSION_VIEW_JOBS)
    if isinstance(portal, RedirectResponse):
        return portal

    job = db.scalar(
        select(Job).where(
            Job.id == job_id,
            Job.tenant_id == portal["tenant_id"],
            Job.subtenant_code == portal["subtenant_code"],
        )
    )
    if job is None:
        return RedirectResponse("/portal/review/jobs", status_code=303)

    endpoint = db.scalar(select(Endpoint).where(Endpoint.id == job.endpoint_id, Endpoint.tenant_id == portal["tenant_id"]))
    version = db.scalar(select(EndpointVersion).where(EndpointVersion.id == job.endpoint_version_id))
    flash_message = request.session.pop("portal_review_flash", None)
    permissions = set(portal["permissions"])
    return templates.TemplateResponse(
        "portal_review_job_detail.html",
        {
            "request": request,
            "portal": portal,
            "job": job,
            "endpoint": endpoint,
            "version": version,
            "flash_message": flash_message,
            "can_add_feedback": PERMISSION_ADD_FEEDBACK in permissions,
            "can_edit_ideal": PERMISSION_EDIT_IDEAL_OUTPUT in permissions,
        },
    )


@router.post("/portal/review/jobs/{job_id}/save")
def portal_review_save_training(
    job_id: str,
    request: Request,
    feedback: str = Form(default=""),
    edited_ideal_output: str = Form(default=""),
    tags: str = Form(default=""),
    save_mode: str = Form(default="full"),
    db: Session = Depends(get_db),
):
    portal = _ensure_portal_permission(request, db, PERMISSION_VIEW_JOBS)
    if isinstance(portal, RedirectResponse):
        return portal

    permissions = set(portal["permissions"])
    if PERMISSION_ADD_FEEDBACK not in permissions:
        request.session["portal_review_flash"] = "You do not have permission to save feedback."
        return RedirectResponse(f"/portal/review/jobs/{job_id}", status_code=303)

    job = db.scalar(
        select(Job).where(
            Job.id == job_id,
            Job.tenant_id == portal["tenant_id"],
            Job.subtenant_code == portal["subtenant_code"],
        )
    )
    if job is None:
        request.session["portal_review_flash"] = "Job not found."
        return RedirectResponse("/portal/review/jobs", status_code=303)

    sanitized_feedback = feedback.strip() or None
    sanitized_edited_output = edited_ideal_output if PERMISSION_EDIT_IDEAL_OUTPUT in permissions else None
    payload = SaveTrainingRequest(
        feedback=sanitized_feedback,
        edited_ideal_output=sanitized_edited_output,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        save_mode="redacted" if save_mode == "redacted" else "full",
    )
    create_training_event_from_job(db, tenant_id=portal["tenant_id"], job=job, payload=payload)
    request.session["portal_review_flash"] = "Training feedback saved."
    return RedirectResponse(f"/portal/review/jobs/{job_id}", status_code=303)


@router.get("/portal/review/training", response_class=HTMLResponse)
def portal_review_training(
    request: Request,
    db: Session = Depends(get_db),
):
    portal = _ensure_portal_permission(request, db, PERMISSION_VIEW_JOBS)
    if isinstance(portal, RedirectResponse):
        return portal
    events = db.scalars(
        select(TrainingEvent)
        .where(
            TrainingEvent.tenant_id == portal["tenant_id"],
            TrainingEvent.subtenant_code == portal["subtenant_code"],
        )
        .order_by(TrainingEvent.created_at.desc())
        .limit(200)
    ).all()
    return templates.TemplateResponse(
        "portal_review_training.html",
        {
            "request": request,
            "portal": portal,
            "events": events,
            "can_export": PERMISSION_EXPORT_TRAINING in set(portal["permissions"]),
        },
    )


@router.post("/portal/review/training/export")
def portal_review_training_export(
    request: Request,
    tags: str = Form(default=""),
    feedback: str = Form(default=""),
    db: Session = Depends(get_db),
):
    portal = _ensure_portal_permission(request, db, PERMISSION_VIEW_JOBS)
    if isinstance(portal, RedirectResponse):
        return portal

    permissions = set(portal["permissions"])
    if PERMISSION_EXPORT_TRAINING not in permissions:
        return RedirectResponse("/portal/review/training", status_code=303)

    payload = TrainingExportRequest(
        subtenant_code=portal["subtenant_code"],
        feedback=feedback.strip() or None,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    )
    events = query_training_events(db, portal["tenant_id"], payload)
    filename = f"portal_training_{portal['subtenant_code']}_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.jsonl"
    return StreamingResponse(
        export_training_jsonl(events),
        media_type="application/jsonl",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
