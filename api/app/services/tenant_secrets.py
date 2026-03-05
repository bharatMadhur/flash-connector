import hashlib
import json
from pathlib import Path

from cryptography.fernet import Fernet

from app.core.config import get_settings


def _build_keyring() -> dict[str, Fernet]:
    settings = get_settings()
    keyring: dict[str, Fernet] = {}

    legacy = (settings.tenant_secret_encryption_key or "").strip()
    if legacy:
        keyring["legacy"] = Fernet(legacy.encode("utf-8"))

    raw_json = (settings.tenant_secret_encryption_keys_json or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("TENANT_SECRET_ENCRYPTION_KEYS_JSON must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("TENANT_SECRET_ENCRYPTION_KEYS_JSON must be a JSON object")
        for key_id, raw_key in parsed.items():
            if not isinstance(key_id, str) or not isinstance(raw_key, str):
                continue
            normalized_id = key_id.strip()
            normalized_key = raw_key.strip()
            if not normalized_id or not normalized_key:
                continue
            keyring[normalized_id] = Fernet(normalized_key.encode("utf-8"))

    if not keyring:
        raise RuntimeError("TENANT_SECRET_ENCRYPTION_KEY (or keyring JSON) is required for tenant-managed keys")
    return keyring


def _active_key_id_and_cipher() -> tuple[str, Fernet]:
    settings = get_settings()
    keyring = _build_keyring()
    active_key_id = (settings.tenant_secret_active_key_id or "").strip()
    if active_key_id:
        cipher = keyring.get(active_key_id)
        if cipher is None:
            raise RuntimeError(f"TENANT_SECRET_ACTIVE_KEY_ID '{active_key_id}' not found in configured keyring")
        return active_key_id, cipher

    # Prefer legacy key if configured for backwards compatibility.
    if "legacy" in keyring:
        return "legacy", keyring["legacy"]

    first_key_id = sorted(keyring.keys())[0]
    return first_key_id, keyring[first_key_id]



def _secret_path(secret_ref: str) -> Path:
    settings = get_settings()
    digest = hashlib.sha256(secret_ref.encode("utf-8")).hexdigest()
    base = Path(settings.tenant_secret_storage_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{digest}.secret"



def put_secret(secret_ref: str, value: str) -> None:
    key_id, cipher = _active_key_id_and_cipher()
    encrypted = cipher.encrypt(value.encode("utf-8")).decode("utf-8")
    envelope = {
        "v": key_id,
        "ct": encrypted,
    }
    path = _secret_path(secret_ref)
    path.write_text(json.dumps(envelope, separators=(",", ":"), ensure_ascii=True), encoding="utf-8")



def get_secret(secret_ref: str) -> str | None:
    path = _secret_path(secret_ref)
    if not path.exists():
        return None
    payload_bytes = path.read_bytes()
    keyring = _build_keyring()

    # New envelope format with key-id metadata.
    try:
        decoded = payload_bytes.decode("utf-8")
        envelope = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        envelope = None

    if isinstance(envelope, dict):
        key_id = envelope.get("v")
        ciphertext = envelope.get("ct")
        if isinstance(key_id, str) and isinstance(ciphertext, str):
            preferred = keyring.get(key_id.strip())
            if preferred is not None:
                return preferred.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
            for cipher in keyring.values():
                try:
                    return cipher.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
                except Exception:  # noqa: BLE001
                    continue
            raise RuntimeError("Unable to decrypt tenant secret with configured keyring")

    # Backwards compatibility for legacy raw-fernet ciphertext files.
    for cipher in keyring.values():
        try:
            return cipher.decrypt(payload_bytes).decode("utf-8")
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("Unable to decrypt legacy tenant secret with configured keyring")



def delete_secret(secret_ref: str) -> None:
    path = _secret_path(secret_ref)
    if path.exists():
        path.unlink()


def rotate_secret(secret_ref: str) -> bool:
    """Re-encrypt an existing secret using the currently active key."""
    current = get_secret(secret_ref)
    if current is None:
        return False
    put_secret(secret_ref, current)
    return True
