from types import SimpleNamespace

from app.models import UserRole
from app.services import oidc as oidc_service


def _mock_settings(
    *,
    role_claim: str = "realm_access.roles",
    tenant_claim: str = "flash_tenant",
    default_role: str = "viewer",
) -> SimpleNamespace:
    return SimpleNamespace(
        oidc_role_claim=role_claim,
        oidc_client_id="flash-console",
        oidc_tenant_claim=tenant_claim,
        default_tenant_name="Default Tenant",
        oidc_default_role=default_role,
        oidc_role_mapping=lambda: {"kc-owner": "owner", "kc-admin": "admin", "kc-dev": "dev", "kc-viewer": "viewer"},
    )


def test_claim_path_nested() -> None:
    claims = {"realm_access": {"roles": ["kc-admin"]}}
    assert oidc_service.claim_path(claims, "realm_access.roles") == ["kc-admin"]
    assert oidc_service.claim_path(claims, "realm_access.missing") is None


def test_extract_role_candidates_with_resource_access_fallback(monkeypatch) -> None:
    monkeypatch.setattr(oidc_service, "get_settings", lambda: _mock_settings())
    claims = {"resource_access": {"flash-console": {"roles": ["kc-dev", "kc-viewer"]}}}
    roles = oidc_service.extract_role_candidates(claims)
    assert roles == ["kc-dev", "kc-viewer"]


def test_map_claims_to_role_priority(monkeypatch) -> None:
    monkeypatch.setattr(oidc_service, "get_settings", lambda: _mock_settings())
    claims = {"realm_access": {"roles": ["kc-viewer", "kc-admin"]}}
    assert oidc_service.map_claims_to_role(claims) == UserRole.admin


def test_resolve_tenant_name_from_claims(monkeypatch) -> None:
    monkeypatch.setattr(oidc_service, "get_settings", lambda: _mock_settings(tenant_claim="tenant_name"))
    claims = {"tenant_name": "Acme Corp"}
    assert oidc_service.resolve_tenant_name_from_claims(claims) == "Acme Corp"


def test_sanitize_next_path() -> None:
    assert oidc_service.sanitize_next_path("/dashboard") == "/dashboard"
    assert oidc_service.sanitize_next_path("https://example.com") == "/dashboard"
    assert oidc_service.sanitize_next_path("//evil") == "/dashboard"
    assert oidc_service.sanitize_next_path("") == "/dashboard"
