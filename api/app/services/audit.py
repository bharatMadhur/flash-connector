"""Audit logging helpers for API and web mutation events."""

from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models import AuditLog

def log_action(
    db: Session,
    tenant_id: str,
    action: str,
    target_type: str,
    target_id: str | None,
    actor_user_id: str | None = None,
    diff_json: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    """Persist one audit event with optional request metadata."""
    ip = request.client.host if request and request.client else None
    user_agent = request.headers.get("user-agent") if request else None
    record = AuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        diff_json=diff_json,
        ip=ip,
        user_agent=user_agent,
    )
    db.add(record)
    db.commit()
