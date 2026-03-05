from types import SimpleNamespace

from app.models import TenantQueryParamsMode
from app.services import tenants as tenant_service


def test_resolve_effective_query_params_merge_and_inherit(monkeypatch) -> None:
    lineage = [
        SimpleNamespace(
            id="child",
            query_params_mode=TenantQueryParamsMode.inherit,
            query_params_json={"ignored": True},
        ),
        SimpleNamespace(
            id="parent",
            query_params_mode=TenantQueryParamsMode.merge,
            query_params_json={"region": "eu", "segment": "growth"},
        ),
        SimpleNamespace(
            id="root",
            query_params_mode=TenantQueryParamsMode.override,
            query_params_json={"locale": "en-US", "region": "us"},
        ),
    ]

    monkeypatch.setattr(tenant_service, "tenant_lineage", lambda db, tenant_id: lineage)
    effective = tenant_service.resolve_effective_query_params(db=None, tenant_id="child")
    assert effective == {"locale": "en-US", "region": "eu", "segment": "growth"}


def test_resolve_effective_query_params_override_replaces_parent(monkeypatch) -> None:
    lineage = [
        SimpleNamespace(
            id="child",
            query_params_mode=TenantQueryParamsMode.merge,
            query_params_json={"channel": "chat"},
        ),
        SimpleNamespace(
            id="parent",
            query_params_mode=TenantQueryParamsMode.override,
            query_params_json={"locale": "fr-FR"},
        ),
        SimpleNamespace(
            id="root",
            query_params_mode=TenantQueryParamsMode.override,
            query_params_json={"locale": "en-US", "tier": "pro"},
        ),
    ]

    monkeypatch.setattr(tenant_service, "tenant_lineage", lambda db, tenant_id: lineage)
    effective = tenant_service.resolve_effective_query_params(db=None, tenant_id="child")
    assert effective == {"locale": "fr-FR", "channel": "chat"}


def test_is_same_or_descendant(monkeypatch) -> None:
    lineage = [
        SimpleNamespace(id="tenant_c"),
        SimpleNamespace(id="tenant_b"),
        SimpleNamespace(id="tenant_a"),
    ]

    monkeypatch.setattr(tenant_service, "tenant_lineage", lambda db, tenant_id: lineage)
    assert tenant_service.is_same_or_descendant(db=None, ancestor_tenant_id="tenant_a", candidate_tenant_id="tenant_c")
    assert not tenant_service.is_same_or_descendant(
        db=None, ancestor_tenant_id="tenant_x", candidate_tenant_id="tenant_c"
    )
