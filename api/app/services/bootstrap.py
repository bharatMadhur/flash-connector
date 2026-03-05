"""Startup bootstrap for default tenant, local login user, and optional API key."""

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import hash_api_key, hash_password, verify_api_key
from app.models import ApiKey, Tenant, User, UserRole


def _ensure_default_tenant(db: Session, settings: Settings) -> Tenant:
    """Create default tenant if missing and return it."""
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
    return tenant


def _ensure_local_auth_user(db: Session, settings: Settings, tenant: Tenant) -> None:
    """Create/update local test user when local auth is enabled."""
    if not settings.local_auth_enabled:
        return

    email = (settings.local_auth_email or "test@local.dev").strip().lower()
    if not email:
        return

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


def _ensure_local_bootstrap_api_key(db: Session, settings: Settings, tenant: Tenant) -> None:
    """Create a stable local API key (if configured) for quick first-run setup."""
    raw_key = (settings.local_bootstrap_api_key or "").strip()
    if not raw_key:
        return

    key_prefix = raw_key[:12]
    if len(key_prefix) < 8:
        return

    candidates = db.scalars(
        select(ApiKey).where(
            ApiKey.tenant_id == tenant.id,
            ApiKey.key_prefix == key_prefix,
        )
    ).all()
    for candidate in candidates:
        if verify_api_key(raw_key, candidate.key_salt, candidate.key_hash):
            if not candidate.is_active:
                candidate.is_active = True
                db.add(candidate)
                db.commit()
            return

    key_salt = secrets.token_hex(16)
    key_hash = hash_api_key(raw_key, key_salt)
    key = ApiKey(
        tenant_id=tenant.id,
        name=(settings.local_bootstrap_api_key_name or "local-bootstrap").strip() or "local-bootstrap",
        key_prefix=key_prefix,
        key_salt=key_salt,
        key_hash=key_hash,
        scopes={"all": True},
        rate_limit_per_min=max(int(settings.local_bootstrap_api_key_rate_limit_per_min), 1),
        monthly_quota=max(int(settings.local_bootstrap_api_key_monthly_quota), 1),
        is_active=True,
    )
    db.add(key)
    db.commit()


def bootstrap_default_tenant(db: Session) -> None:
    """Run idempotent startup bootstrap routines for local/self-hosted installs."""
    settings = get_settings()
    tenant = _ensure_default_tenant(db, settings)
    _ensure_local_auth_user(db, settings, tenant)
    _ensure_local_bootstrap_api_key(db, settings, tenant)
