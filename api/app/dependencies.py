"""Request dependencies for session auth, API key auth, and CSRF protection."""

from dataclasses import dataclass
import hmac
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models import User
from app.services.api_keys import ApiKeyContext, resolve_api_key
from app.services.tenants import is_same_or_descendant


@dataclass
class SessionUser:
    user_id: str
    tenant_id: str
    principal_tenant_id: str
    role: str
    email: str | None = None
    display_name: str | None = None


CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def ensure_csrf_token(request: Request) -> str:
    """Return existing CSRF token or create one for the current session."""
    token = request.session.get("csrf_token")
    if isinstance(token, str) and token.strip():
        return token
    generated = secrets.token_urlsafe(32)
    request.session["csrf_token"] = generated
    return generated


async def csrf_protect(request: Request) -> None:
    """Enforce CSRF validation for session-authenticated unsafe methods."""
    if request.method.upper() not in CSRF_UNSAFE_METHODS:
        return

    # Public API requests are authenticated via x-api-key, not cookie sessions.
    if request.headers.get("x-api-key"):
        return

    session = request.session
    if not session.get("user_id"):
        return

    expected_token = ensure_csrf_token(request)
    supplied_token = request.headers.get("x-csrf-token") or request.headers.get("x-csrftoken")

    if not supplied_token:
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            try:
                form = await request.form()
                form_value = form.get("csrf_token")
                if isinstance(form_value, str):
                    supplied_token = form_value
            except Exception:  # noqa: BLE001
                supplied_token = None

    if not supplied_token or not hmac.compare_digest(supplied_token, expected_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token missing or invalid")


def _get_session_user(request: Request) -> SessionUser | None:
    """Build session user object from cookie session values."""
    session = request.session
    user_id = session.get("user_id")
    principal_tenant_id = session.get("principal_tenant_id") or session.get("tenant_id")
    tenant_id = session.get("active_tenant_id") or principal_tenant_id
    role = session.get("role")
    if not user_id or not tenant_id or not principal_tenant_id or not role:
        return None
    return SessionUser(
        user_id=user_id,
        tenant_id=tenant_id,
        principal_tenant_id=principal_tenant_id,
        role=role,
        email=session.get("email"),
        display_name=session.get("display_name"),
    )


def _normalize_role_value(raw_role: object) -> str:
    """Normalize enum/string role values to plain string."""
    if hasattr(raw_role, "value"):
        value = getattr(raw_role, "value")
        if isinstance(value, str):
            return value
    if isinstance(raw_role, str):
        return raw_role
    return "viewer"


def _validate_session_user(
    request: Request,
    db: Session,
    session_user: SessionUser | None,
) -> SessionUser | None:
    """Validate session user against database and tenant hierarchy."""
    if session_user is None:
        return None

    user = db.get(User, session_user.user_id)
    if user is None or user.tenant_id != session_user.principal_tenant_id:
        request.session.clear()
        return None

    if not is_same_or_descendant(db, session_user.principal_tenant_id, session_user.tenant_id):
        request.session.clear()
        return None

    return SessionUser(
        user_id=user.id,
        tenant_id=session_user.tenant_id,
        principal_tenant_id=session_user.principal_tenant_id,
        role=_normalize_role_value(user.role),
        email=user.email,
        display_name=user.display_name,
    )

def get_optional_session_user(
    request: Request,
    db: Session = Depends(get_db),
) -> SessionUser | None:
    """Return validated session user if available, else None."""
    return _validate_session_user(request, db, _get_session_user(request))



def get_session_user(request: Request, db: Session = Depends(get_db)) -> SessionUser:
    """Return validated session user or raise 401."""
    raw_session_user = _get_session_user(request)
    validated = _validate_session_user(request, db, raw_session_user)
    if validated is None:
        detail = "Login required" if raw_session_user is None else "Session is invalid"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
    return validated



def require_roles(*allowed_roles: str):
    """Factory that returns role-based access dependency."""
    def _dependency(session_user: SessionUser = Depends(get_session_user)) -> SessionUser:
        if session_user.role not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return session_user

    return _dependency



def get_api_key_context(
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
    db: Session = Depends(get_db),
) -> ApiKeyContext:
    """Resolve API key context from `x-api-key` header or raise 401."""
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing x-api-key")

    ctx = resolve_api_key(db, x_api_key)
    if ctx is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return ctx



def get_optional_api_key_context(
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
    db: Session = Depends(get_db),
) -> ApiKeyContext | None:
    """Resolve API key context when header is present, otherwise None."""
    if not x_api_key:
        return None
    return resolve_api_key(db, x_api_key)
