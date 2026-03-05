from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.security import generate_portal_token, verify_portal_token
from app.models import PortalLink
from app.schemas.portal import PortalLinkCreate

PERMISSION_VIEW_JOBS = "view_jobs"
PERMISSION_ADD_FEEDBACK = "add_feedback"
PERMISSION_EDIT_IDEAL_OUTPUT = "edit_ideal_output"
PERMISSION_EXPORT_TRAINING = "export_training"

ALL_PORTAL_PERMISSIONS = {
    PERMISSION_VIEW_JOBS,
    PERMISSION_ADD_FEEDBACK,
    PERMISSION_EDIT_IDEAL_OUTPUT,
    PERMISSION_EXPORT_TRAINING,
}


@dataclass(frozen=True)
class PortalAccessResult:
    ok: bool
    reason: str
    link: PortalLink | None


def _normalize_permissions(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = (value or "").strip()
        if cleaned not in ALL_PORTAL_PERMISSIONS:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    if PERMISSION_VIEW_JOBS not in seen:
        ordered.insert(0, PERMISSION_VIEW_JOBS)
    return ordered


def list_portal_links(db: Session, tenant_id: str) -> list[PortalLink]:
    return db.scalars(
        select(PortalLink)
        .where(PortalLink.tenant_id == tenant_id)
        .order_by(PortalLink.created_at.desc())
    ).all()


def get_portal_link(db: Session, tenant_id: str, link_id: str) -> PortalLink | None:
    return db.scalar(
        select(PortalLink).where(
            PortalLink.id == link_id,
            PortalLink.tenant_id == tenant_id,
        )
    )


def create_portal_link(
    db: Session,
    *,
    tenant_id: str,
    created_by_user_id: str | None,
    payload: PortalLinkCreate,
) -> tuple[PortalLink, str]:
    raw_token, token_prefix, token_hash = generate_portal_token()
    permissions = _normalize_permissions(payload.permissions)
    link = PortalLink(
        tenant_id=tenant_id,
        subtenant_code=payload.subtenant_code.strip(),
        token_prefix=token_prefix,
        token_hash=token_hash,
        permissions_json={"permissions": permissions},
        expires_at=payload.expires_at,
        is_revoked=False,
        created_by_user_id=created_by_user_id,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link, raw_token


def revoke_portal_link(db: Session, link: PortalLink) -> PortalLink:
    link.is_revoked = True
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def resolve_portal_token(db: Session, raw_token: str) -> PortalAccessResult:
    prefix = (raw_token or "").strip()[:16]
    if not prefix:
        return PortalAccessResult(ok=False, reason="Missing token.", link=None)

    candidates = db.scalars(
        select(PortalLink).where(
            and_(
                PortalLink.token_prefix == prefix,
                PortalLink.is_revoked.is_(False),
            )
        )
    ).all()
    if not candidates:
        return PortalAccessResult(ok=False, reason="Portal link not found.", link=None)

    now = datetime.now(UTC)
    for candidate in candidates:
        if not verify_portal_token(raw_token, candidate.token_hash):
            continue
        if candidate.expires_at <= now:
            return PortalAccessResult(ok=False, reason="Portal link has expired.", link=None)
        candidate.last_used_at = now
        db.add(candidate)
        db.commit()
        db.refresh(candidate)
        return PortalAccessResult(ok=True, reason="ok", link=candidate)

    return PortalAccessResult(ok=False, reason="Portal link is invalid.", link=None)


def link_permissions(link: PortalLink) -> set[str]:
    permissions = link.permissions_json.get("permissions") if isinstance(link.permissions_json, dict) else []
    if not isinstance(permissions, list):
        permissions = []
    return set(_normalize_permissions([str(item) for item in permissions]))
