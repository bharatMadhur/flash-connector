from __future__ import annotations

from collections import deque
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Tenant, TenantQueryParamsMode


def get_tenant(db: Session, tenant_id: str) -> Tenant | None:
    return db.scalar(select(Tenant).where(Tenant.id == tenant_id))


def tenant_lineage(db: Session, tenant_id: str, *, max_depth: int | None = None) -> list[Tenant]:
    if max_depth is None:
        max_depth = max(int(get_settings().tenant_hierarchy_max_depth), 1)
    lineage: list[Tenant] = []
    current_id: str | None = tenant_id
    depth = 0
    while current_id and depth < max_depth:
        tenant = get_tenant(db, current_id)
        if tenant is None:
            break
        lineage.append(tenant)
        current_id = tenant.parent_tenant_id
        depth += 1
    return lineage


def is_same_or_descendant(db: Session, ancestor_tenant_id: str, candidate_tenant_id: str) -> bool:
    if ancestor_tenant_id == candidate_tenant_id:
        return True
    for tenant in tenant_lineage(db, candidate_tenant_id):
        if tenant.id == ancestor_tenant_id:
            return True
    return False


def list_accessible_tenants(db: Session, root_tenant_id: str) -> list[Tenant]:
    by_id: dict[str, Tenant] = {}
    queue: deque[str] = deque([root_tenant_id])

    while queue:
        tenant_id = queue.popleft()
        tenant = get_tenant(db, tenant_id)
        if tenant is None or tenant.id in by_id:
            continue
        by_id[tenant.id] = tenant

        child_ids = db.scalars(select(Tenant.id).where(Tenant.parent_tenant_id == tenant.id).order_by(Tenant.name.asc())).all()
        queue.extend(child_ids)

    return sorted(by_id.values(), key=lambda t: (t.parent_tenant_id or "", t.name.lower()))


def build_tenant_breadcrumb(db: Session, tenant_id: str) -> list[Tenant]:
    lineage = tenant_lineage(db, tenant_id)
    lineage.reverse()
    return lineage


def resolve_effective_query_params(db: Session, tenant_id: str) -> dict[str, Any]:
    lineage = tenant_lineage(db, tenant_id)
    if not lineage:
        return {}

    # Evaluate root -> child to keep overrides deterministic.
    lineage.reverse()
    effective: dict[str, Any] = {}

    for tenant in lineage:
        own = tenant.query_params_json if isinstance(tenant.query_params_json, dict) else {}
        mode = tenant.query_params_mode

        if mode == TenantQueryParamsMode.inherit:
            continue
        if mode == TenantQueryParamsMode.override:
            effective = dict(own)
            continue
        if mode == TenantQueryParamsMode.merge:
            effective = {**effective, **own}
            continue

    return effective
