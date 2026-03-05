"""Session/auth API router for login/logout/session status endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.dependencies import SessionUser, csrf_protect, get_optional_session_user
from app.models import User

router = APIRouter(prefix="/v1/auth", tags=["auth"], dependencies=[Depends(csrf_protect)])


@router.post("/login")
def login_disabled() -> dict:
    """Legacy password login endpoint intentionally disabled in OIDC-first flow."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Password login is disabled. Use OIDC via /login and Keycloak.",
    )


@router.post("/logout")
def logout(request: Request) -> dict:
    """Clear session and return success marker."""
    request.session.clear()
    return {"ok": True}


@router.get("/session")
def session_info(
    db: Session = Depends(get_db),
    session_user: SessionUser | None = Depends(get_optional_session_user),
) -> dict:
    """Return authenticated session identity details."""
    if session_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    user = db.get(User, session_user.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is invalid")

    return {
        "user_id": user.id,
        "tenant_id": session_user.tenant_id,
        "principal_tenant_id": session_user.principal_tenant_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role.value,
    }
