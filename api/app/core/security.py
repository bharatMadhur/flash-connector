"""Password, API key, job id, and portal token security helpers."""

import hashlib
import hmac
import secrets

from passlib.context import CryptContext

from app.core.config import get_settings

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    """Hash a user password for persistent storage."""
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify plain password against stored password hash."""
    return pwd_context.verify(password, password_hash)


def hash_api_key(raw_key: str, key_salt: str) -> str:
    """Hash a virtual API key using server HMAC secret + per-key salt."""
    settings = get_settings()
    return hmac.new(
        settings.api_key_hmac_secret.encode("utf-8"),
        f"{key_salt}:{raw_key}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_api_key() -> tuple[str, str, str, str]:
    """Generate raw key + metadata tuple used to persist an API key record."""
    raw_key = f"fc_{secrets.token_urlsafe(32)}"
    key_prefix = raw_key[:12]
    key_salt = secrets.token_hex(16)
    key_hash = hash_api_key(raw_key, key_salt)
    return raw_key, key_prefix, key_salt, key_hash


def verify_api_key(raw_key: str, key_salt: str, key_hash: str) -> bool:
    """Constant-time API key verification helper."""
    expected = hash_api_key(raw_key, key_salt)
    return hmac.compare_digest(expected, key_hash)


def generate_job_id() -> str:
    """Generate externally-visible job id used by async submit/poll APIs."""
    return f"job_{secrets.token_urlsafe(10).replace('-', '').replace('_', '')}"


def generate_portal_token() -> tuple[str, str, str]:
    """Generate raw sub-tenant portal token + prefix + hash."""
    settings = get_settings()
    raw_token = f"pl_{secrets.token_urlsafe(36)}"
    token_prefix = raw_token[:16]
    token_hash = hmac.new(
        settings.api_key_hmac_secret.encode("utf-8"),
        raw_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return raw_token, token_prefix, token_hash


def verify_portal_token(raw_token: str, token_hash: str) -> bool:
    """Constant-time portal token verification helper."""
    settings = get_settings()
    expected = hmac.new(
        settings.api_key_hmac_secret.encode("utf-8"),
        raw_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, token_hash)
