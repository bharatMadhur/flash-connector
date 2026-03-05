"""FastAPI application entrypoint and middleware wiring."""

from contextlib import asynccontextmanager
import logging
from pathlib import Path
import re

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.routers import admin, auth, public, web
from app.services.audit import log_action
from app.services.bootstrap import bootstrap_default_tenant

logger = logging.getLogger(__name__)


def _is_unset_or_default(value: str | None, default_value: str) -> bool:
    """Return True when a secret/config value is missing or left at default."""
    return not value or value.strip() == default_value


def _validate_runtime_configuration() -> None:
    """Validate runtime safety constraints and fail fast in production mode."""
    issues: list[str] = []
    warnings: list[str] = []
    is_production_mode = settings.is_production_mode()

    if _is_unset_or_default(settings.session_secret, "change-me-session-secret"):
        issues.append("SESSION_SECRET is not configured")
    if _is_unset_or_default(settings.api_key_hmac_secret, "change-me-api-key-secret"):
        issues.append("API_KEY_HMAC_SECRET is not configured")
    if not (settings.tenant_secret_encryption_key or "").strip():
        issues.append("TENANT_SECRET_ENCRYPTION_KEY is not configured")

    if is_production_mode:
        if settings.local_auth_enabled:
            issues.append("LOCAL_AUTH_ENABLED must be false in production mode")
        if (settings.local_bootstrap_api_key or "").strip():
            issues.append("LOCAL_BOOTSTRAP_API_KEY must not be set in production mode")
        if not settings.session_cookie_secure:
            issues.append("SESSION_COOKIE_SECURE must be true in production mode")
        if any(origin.strip() == "*" for origin in settings.cors_origin_list()):
            issues.append("CORS_ORIGINS must not include '*' in production mode")
        if settings.rate_limit_allow_in_memory_fallback_nonprod:
            issues.append("RATE_LIMIT_ALLOW_IN_MEMORY_FALLBACK_NONPROD must be false in production mode")
        if not settings.oidc_enabled():
            issues.append("OIDC must be configured in production mode")

    if is_production_mode and issues:
        raise RuntimeError(
            "Invalid runtime configuration for production mode: " + "; ".join(sorted(issues))
        )

    if settings.environment in {"development", "test"} and settings.runtime_mode != "production":
        warnings = issues
    if warnings:
        logger.warning("Startup configuration warnings: %s", "; ".join(sorted(warnings)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.bootstrap_ok = False
    app.state.bootstrap_error = None
    _validate_runtime_configuration()
    db = SessionLocal()
    try:
        bootstrap_default_tenant(db)
        app.state.bootstrap_ok = True
        app.state.bootstrap_error = None
    except Exception:  # noqa: BLE001
        # Keep the app available even if DB is temporarily unavailable at boot.
        logger.exception("bootstrap_default_tenant failed during startup")
        app.state.bootstrap_ok = False
        app.state.bootstrap_error = "bootstrap_default_tenant failed"
    finally:
        db.close()
    yield


settings = get_settings()
app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie=settings.session_cookie_name,
    same_site=settings.session_cookie_samesite,
    https_only=settings.session_cookie_secure,
    max_age=settings.session_cookie_max_age,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(public.router)
app.include_router(admin.router)
app.include_router(web.router)

app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "service": settings.app_name,
        "runtime_mode": settings.runtime_mode,
        "environment": settings.environment,
        "bootstrap_ok": bool(getattr(app.state, "bootstrap_ok", False)),
        "bootstrap_error": getattr(app.state, "bootstrap_error", None),
    }


@app.get("/readyz")
def readyz():
    if bool(getattr(app.state, "bootstrap_ok", False)):
        return {
            "status": "ready",
            "service": settings.app_name,
            "runtime_mode": settings.runtime_mode,
            "environment": settings.environment,
        }
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "not_ready",
            "service": settings.app_name,
            "reason": getattr(app.state, "bootstrap_error", "startup not completed"),
        },
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> RedirectResponse:
    return RedirectResponse(url="/static/favicon.svg?v=20260305-6")


_ID_LIKE_PATTERN = re.compile(r"^(job_[A-Za-z0-9]+|[0-9a-fA-F-]{8,})$")
_AUDITED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _extract_target_from_path(path: str) -> tuple[str, str | None]:
    parts = [item for item in path.strip("/").split("/") if item]
    if not parts:
        return "root", None
    target_type = parts[0]
    target_id: str | None = None
    for part in parts[1:]:
        if _ID_LIKE_PATTERN.match(part):
            target_id = part
            break
    return target_type, target_id


@app.middleware("http")
async def audit_web_mutations(request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)

    method = request.method.upper()
    path = request.url.path
    if method not in _AUDITED_METHODS:
        return response
    if path.startswith("/v1/") or path.startswith("/static/"):
        return response
    if response.status_code >= 400:
        return response

    session = getattr(request, "session", {})
    if not isinstance(session, dict):
        return response
    actor_user_id = session.get("user_id")
    tenant_id = session.get("active_tenant_id") or session.get("tenant_id")
    if not actor_user_id or not tenant_id:
        return response

    target_type, target_id = _extract_target_from_path(path)
    db = SessionLocal()
    try:
        log_action(
            db,
            tenant_id=str(tenant_id),
            actor_user_id=str(actor_user_id),
            action=f"web.{method.lower()}",
            target_type=target_type,
            target_id=target_id,
            diff_json={"path": path, "status_code": response.status_code},
            request=request,
        )
    except Exception:  # noqa: BLE001
        logger.exception("audit_web_mutations failed", extra={"path": path, "method": method})
    finally:
        db.close()

    return response
