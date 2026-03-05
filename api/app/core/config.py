"""Application settings and environment variable parsing.

This module centralizes all runtime configuration so API, worker, and scripts
share one typed source of truth.
"""

import json
from functools import lru_cache
from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed environment-backed settings used across API and worker."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "flash-connector"
    environment: Literal["development", "staging", "production", "test"] = "development"
    runtime_mode: Literal["sandbox", "production"] = "sandbox"
    debug: bool = False

    database_url: str = "postgresql+psycopg2://flash:flash@postgres:5432/flash_connector"
    redis_url: str = "redis://redis:6379/0"

    session_secret: str = "change-me-session-secret"
    session_cookie_name: str = "flash_connector_session"
    session_cookie_secure: bool = True
    session_cookie_samesite: str = "lax"
    session_cookie_max_age: int = 60 * 60 * 24 * 14

    api_key_hmac_secret: str = "change-me-api-key-secret"

    oidc_issuer_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://localhost:8000/auth/callback"
    oidc_post_logout_redirect_uri: str = "http://localhost:8000/login"
    oidc_scopes: str = "openid profile email"
    oidc_role_claim: str = "realm_access.roles"
    oidc_email_claim: str = "email"
    oidc_name_claim: str = "name"
    oidc_tenant_claim: str = "flash_tenant"
    oidc_role_mapping_json: str = '{"owner":"owner","admin":"admin","dev":"dev","viewer":"viewer"}'
    oidc_default_role: Literal["owner", "admin", "dev", "viewer"] = "viewer"
    oidc_auto_create_tenant: bool = False
    oidc_metadata_cache_seconds: int = 300
    local_auth_enabled: bool = False
    local_auth_username: str = "test"
    local_auth_password: str = "test"
    local_auth_email: str = "test@local.dev"
    local_auth_role: Literal["owner", "admin", "dev", "viewer"] = "owner"
    local_bootstrap_api_key: str = ""
    local_bootstrap_api_key_name: str = "local-bootstrap"
    local_bootstrap_api_key_rate_limit_per_min: int = 600
    local_bootstrap_api_key_monthly_quota: int = 1000000

    openai_api_key: str = ""
    azure_openai_api_key: str = ""
    azure_openai_base_url: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = ""
    azure_ai_foundry_api_key: str = ""
    azure_ai_foundry_base_url: str = ""
    azure_ai_foundry_endpoint: str = ""
    azure_ai_foundry_api_version: str = ""
    platform_provider_keys_json: str = "{}"
    tenant_secret_storage_dir: str = "/tmp/flash_connector_secrets"
    tenant_secret_encryption_key: str = ""
    tenant_secret_encryption_keys_json: str = "{}"
    tenant_secret_active_key_id: str = ""

    cors_origins: str = "http://localhost:8000"

    single_tenant_mode: bool = False
    default_tenant_name: str = "Default Tenant"

    queue_name: str = "jobs"
    job_timeout_seconds: int = 180
    max_concurrency: int = 1
    provider_batch_poll_interval_seconds: int = 20
    provider_batch_max_poll_attempts: int = 720
    rate_limit_allow_in_memory_fallback_nonprod: bool = True
    api_key_last_used_update_interval_seconds: int = 300
    tenant_hierarchy_max_depth: int = 8

    @field_validator("cors_origins")
    @classmethod
    def validate_origins(cls, value: str) -> str:
        """Normalize CORS origins raw value before parsing."""
        return value.strip()

    def cors_origin_list(self) -> list[str]:
        """Return configured CORS origins as a normalized list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def oidc_enabled(self) -> bool:
        """Return whether OIDC login flow is fully configured."""
        return bool(self.oidc_issuer_url.strip() and self.oidc_client_id.strip())

    def oidc_scope_list(self) -> list[str]:
        """Return configured OIDC scopes."""
        return [scope.strip() for scope in self.oidc_scopes.split(" ") if scope.strip()]

    def oidc_role_mapping(self) -> dict[str, str]:
        """Return safe external-role -> internal-role mapping."""
        try:
            parsed: Any = json.loads(self.oidc_role_mapping_json)
        except json.JSONDecodeError:
            return {"owner": "owner", "admin": "admin", "dev": "dev", "viewer": "viewer"}

        if not isinstance(parsed, dict):
            return {"owner": "owner", "admin": "admin", "dev": "dev", "viewer": "viewer"}

        allowed = {"owner", "admin", "dev", "viewer"}
        normalized: dict[str, str] = {}
        for ext_role, internal_role in parsed.items():
            if not isinstance(ext_role, str) or not isinstance(internal_role, str):
                continue
            if internal_role not in allowed:
                continue
            normalized[ext_role] = internal_role
        return normalized or {"owner": "owner", "admin": "admin", "dev": "dev", "viewer": "viewer"}

    def is_production_mode(self) -> bool:
        """Return True when strict production safety profile is requested."""
        return self.runtime_mode == "production" or self.environment in {"staging", "production"}

    def is_sandbox_mode(self) -> bool:
        """Return True when local/dev relaxed behavior is allowed."""
        return not self.is_production_mode()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance for the current process."""
    return Settings()
