import base64
import hashlib
import secrets
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password
from app.models import Tenant, User, UserRole


class OidcAuthError(RuntimeError):
    pass


_DISCOVERY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JWK_CLIENTS: dict[str, Any] = {}


def _well_known_url(issuer_url: str) -> str:
    stripped = issuer_url.strip().rstrip("/")
    if stripped.endswith("/.well-known/openid-configuration"):
        return stripped
    return f"{stripped}/.well-known/openid-configuration"


def get_oidc_metadata() -> dict[str, Any]:
    settings = get_settings()
    if not settings.oidc_enabled():
        raise OidcAuthError("OIDC is not configured. Set OIDC_ISSUER_URL and OIDC_CLIENT_ID.")

    cache_key = settings.oidc_issuer_url.strip()
    now = time.time()
    cached = _DISCOVERY_CACHE.get(cache_key)
    if cached and now - cached[0] < max(settings.oidc_metadata_cache_seconds, 30):
        return cached[1]

    metadata_url = _well_known_url(settings.oidc_issuer_url)
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            response = client.get(metadata_url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
        raise OidcAuthError(f"Failed to load OIDC metadata from {metadata_url}: {exc}") from exc

    required = ["issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"]
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise OidcAuthError(f"OIDC metadata missing required fields: {', '.join(missing)}")

    _DISCOVERY_CACHE[cache_key] = (now, payload)
    return payload


def generate_code_verifier() -> str:
    verifier = secrets.token_urlsafe(96)
    if len(verifier) < 43:
        verifier = verifier + ("x" * (43 - len(verifier)))
    return verifier[:128]


def build_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def build_authorization_url(*, state: str, nonce: str, code_challenge: str) -> str:
    settings = get_settings()
    metadata = get_oidc_metadata()
    query = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": " ".join(settings.oidc_scope_list()),
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }
    return f"{metadata['authorization_endpoint']}?{urlencode(query)}"


def exchange_code_for_tokens(*, code: str, code_verifier: str) -> dict[str, Any]:
    settings = get_settings()
    metadata = get_oidc_metadata()
    payload: dict[str, str] = {
        "grant_type": "authorization_code",
        "client_id": settings.oidc_client_id,
        "code": code,
        "redirect_uri": settings.oidc_redirect_uri,
        "code_verifier": code_verifier,
    }
    if settings.oidc_client_secret:
        payload["client_secret"] = settings.oidc_client_secret

    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            response = client.post(
                metadata["token_endpoint"],
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.RequestError as exc:
        raise OidcAuthError(f"Token exchange failed: {exc}") from exc

    if response.status_code >= 400:
        try:
            data = response.json()
        except ValueError:
            data = {"error_description": response.text}
        reason = data.get("error_description") or data.get("error") or "unknown error"
        raise OidcAuthError(f"Token exchange failed ({response.status_code}): {reason}")

    try:
        token_payload = response.json()
    except ValueError as exc:
        raise OidcAuthError("Token endpoint returned non-JSON response.") from exc

    if not token_payload.get("id_token"):
        raise OidcAuthError("Token endpoint did not return an id_token.")
    return token_payload


def _jwk_client(jwks_uri: str) -> Any:
    try:
        from jwt import PyJWKClient
    except ImportError as exc:
        raise OidcAuthError("PyJWT is required for OIDC token validation. Install api dependencies.") from exc

    client = _JWK_CLIENTS.get(jwks_uri)
    if client is not None:
        return client
    created = PyJWKClient(jwks_uri, cache_keys=True)
    _JWK_CLIENTS[jwks_uri] = created
    return created


def parse_and_validate_id_token(id_token: str, *, expected_nonce: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    metadata = get_oidc_metadata()
    try:
        import jwt
    except ImportError as exc:
        raise OidcAuthError("PyJWT is required for OIDC token validation. Install api dependencies.") from exc

    try:
        signing_key = _jwk_client(metadata["jwks_uri"]).get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token,
            signing_key,
            algorithms=["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"],
            audience=settings.oidc_client_id,
            issuer=metadata["issuer"],
            options={"require": ["sub", "iss", "exp", "iat"]},
        )
    except Exception as exc:  # noqa: BLE001
        raise OidcAuthError(f"Failed to validate id_token: {exc}") from exc

    if expected_nonce:
        nonce = claims.get("nonce")
        if nonce != expected_nonce:
            raise OidcAuthError("OIDC nonce mismatch.")

    return claims


def claim_path(claims: dict[str, Any], path: str) -> Any:
    if not path:
        return None
    current: Any = claims
    for piece in path.split("."):
        if isinstance(current, dict):
            current = current.get(piece)
            continue
        return None
    return current


def extract_role_candidates(claims: dict[str, Any]) -> list[str]:
    settings = get_settings()
    raw = claim_path(claims, settings.oidc_role_claim)
    roles: list[str] = []

    if isinstance(raw, str):
        roles.append(raw)
    elif isinstance(raw, list):
        roles.extend([value for value in raw if isinstance(value, str)])
    elif isinstance(raw, dict):
        nested = raw.get("roles")
        if isinstance(nested, list):
            roles.extend([value for value in nested if isinstance(value, str)])

    if not roles:
        fallback = claim_path(claims, f"resource_access.{settings.oidc_client_id}.roles")
        if isinstance(fallback, list):
            roles.extend([value for value in fallback if isinstance(value, str)])

    deduped: list[str] = []
    seen: set[str] = set()
    for role in roles:
        if role in seen:
            continue
        seen.add(role)
        deduped.append(role)
    return deduped


def map_claims_to_role(claims: dict[str, Any]) -> UserRole:
    settings = get_settings()
    mapping = settings.oidc_role_mapping()
    mapped: set[str] = set()
    for external_role in extract_role_candidates(claims):
        internal = mapping.get(external_role)
        if internal:
            mapped.add(internal)

    for role in ("owner", "admin", "dev", "viewer"):
        if role in mapped:
            return UserRole(role)
    return UserRole(settings.oidc_default_role)


def resolve_tenant_name_from_claims(claims: dict[str, Any]) -> str:
    settings = get_settings()
    value = claim_path(claims, settings.oidc_tenant_claim)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return settings.default_tenant_name


def _claim_string(claims: dict[str, Any], path: str) -> str | None:
    value = claim_path(claims, path)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _ensure_tenant(db: Session, tenant_name: str) -> Tenant:
    settings = get_settings()
    tenant = db.scalar(select(Tenant).where(Tenant.name == tenant_name))
    if tenant is not None:
        return tenant

    if settings.single_tenant_mode and tenant_name != settings.default_tenant_name:
        raise OidcAuthError(
            f"Tenant '{tenant_name}' from OIDC claim is not allowed in single-tenant mode."
        )

    if not settings.oidc_auto_create_tenant and tenant_name != settings.default_tenant_name:
        raise OidcAuthError(
            f"Tenant '{tenant_name}' not found. Enable OIDC_AUTO_CREATE_TENANT or create this tenant first."
        )

    tenant = Tenant(
        name=tenant_name,
        can_create_subtenants=True,
    )
    db.add(tenant)
    db.flush()
    return tenant


def provision_user_from_claims(db: Session, claims: dict[str, Any]) -> tuple[User, Tenant]:
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise OidcAuthError("OIDC id_token is missing a valid subject.")

    issuer = claims.get("iss")
    if not isinstance(issuer, str) or not issuer.strip():
        issuer = get_settings().oidc_issuer_url

    tenant_name = resolve_tenant_name_from_claims(claims)
    tenant = _ensure_tenant(db, tenant_name)

    email = _claim_string(claims, get_settings().oidc_email_claim)
    if email is None:
        email = f"{subject}@oidc.local"
    email = email.lower()

    display_name = _claim_string(claims, get_settings().oidc_name_claim)
    role = map_claims_to_role(claims)

    user = db.scalar(
        select(User).where(
            User.tenant_id == tenant.id,
            User.oidc_issuer == issuer,
            User.oidc_subject == subject,
        )
    )
    if user is None:
        user = db.scalar(select(User).where(User.tenant_id == tenant.id, User.email == email))

    if user is None:
        user = User(
            tenant_id=tenant.id,
            email=email,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            role=role,
            display_name=display_name,
            oidc_issuer=issuer,
            oidc_subject=subject,
            last_login_at=datetime.now(UTC),
        )
        db.add(user)
    else:
        user.email = email
        user.role = role
        user.display_name = display_name
        user.oidc_issuer = issuer
        user.oidc_subject = subject
        user.last_login_at = datetime.now(UTC)
        db.add(user)

    db.commit()
    db.refresh(user)
    db.refresh(tenant)
    return user, tenant


def build_logout_url(id_token_hint: str | None = None) -> str:
    settings = get_settings()
    metadata = get_oidc_metadata()
    end_session_endpoint = metadata.get("end_session_endpoint")
    redirect_uri = settings.oidc_post_logout_redirect_uri or settings.oidc_redirect_uri
    if not end_session_endpoint:
        return redirect_uri

    query = {
        "client_id": settings.oidc_client_id,
        "post_logout_redirect_uri": redirect_uri,
    }
    if id_token_hint:
        query["id_token_hint"] = id_token_hint
    return f"{end_session_endpoint}?{urlencode(query)}"


def sanitize_next_path(raw_path: str | None) -> str:
    if not raw_path:
        return "/dashboard"
    cleaned = raw_path.strip()
    if not cleaned.startswith("/"):
        return "/dashboard"
    if cleaned.startswith("//"):
        return "/dashboard"
    return cleaned
