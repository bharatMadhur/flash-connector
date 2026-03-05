"""Virtual API key creation, resolution, and endpoint-scope checks."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import generate_api_key, verify_api_key
from app.models import ApiKey
from app.schemas.api_keys import ApiKeyCreate, ApiKeyScopes


@dataclass
class ApiKeyContext:
    """Resolved API key context used for authenticated public API requests."""

    api_key_id: str
    tenant_id: str
    scopes: ApiKeyScopes
    rate_limit_per_min: int
    monthly_quota: int

def create_virtual_key(db: Session, tenant_id: str, payload: ApiKeyCreate) -> tuple[ApiKey, str]:
    """Create a virtual API key record and return persisted row + raw key."""
    raw_key, prefix, salt, key_hash = generate_api_key()
    key = ApiKey(
        tenant_id=tenant_id,
        name=payload.name,
        key_prefix=prefix,
        key_salt=salt,
        key_hash=key_hash,
        scopes=payload.scopes.model_dump(),
        rate_limit_per_min=payload.rate_limit_per_min,
        monthly_quota=payload.monthly_quota,
        is_active=True,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key, raw_key

def resolve_api_key(db: Session, raw_key: str) -> ApiKeyContext | None:
    """Resolve raw key to active context and update `last_used_at` with debounce."""
    settings = get_settings()
    update_interval = max(int(settings.api_key_last_used_update_interval_seconds), 0)
    key_prefix = raw_key[:12]
    candidates = db.scalars(
        select(ApiKey).where(ApiKey.key_prefix == key_prefix, ApiKey.is_active.is_(True))
    ).all()

    for candidate in candidates:
        if verify_api_key(raw_key, candidate.key_salt, candidate.key_hash):
            now = datetime.now(UTC)
            if (
                update_interval == 0
                or candidate.last_used_at is None
                or (now - candidate.last_used_at).total_seconds() >= update_interval
            ):
                candidate.last_used_at = now
                db.add(candidate)
                db.commit()
            scopes = ApiKeyScopes.model_validate(candidate.scopes or {"all": True})
            return ApiKeyContext(
                api_key_id=candidate.id,
                tenant_id=candidate.tenant_id,
                scopes=scopes,
                rate_limit_per_min=candidate.rate_limit_per_min,
                monthly_quota=candidate.monthly_quota,
            )

    return None

def key_allows_endpoint(scopes: ApiKeyScopes, endpoint_id: str) -> bool:
    """Return whether key scopes allow access to a specific endpoint id."""
    if scopes.all is True:
        return True
    return endpoint_id in scopes.endpoint_ids
