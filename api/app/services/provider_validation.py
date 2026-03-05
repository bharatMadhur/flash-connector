"""Provider key validation probes used by connection save flows.

The checks are intentionally lightweight and conservative:
- definitive failures for clear auth/base issues
- inconclusive success for transient upstream/network errors
"""

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.provider_catalog import ensure_supported_provider_slug
from app.core.provider_profiles import azure_provider_mode, is_azure_provider_slug
from app.services.providers import resolve_provider_endpoint_options


@dataclass(frozen=True)
class ProviderKeyValidationResult:
    """Result payload returned by provider key validation."""

    valid: bool
    definitive: bool
    message: str


def _http_get(
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    timeout_seconds: int = 8,
) -> httpx.Response:
    """Execute a bounded GET request for provider validation probes."""
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
        return client.get(url, headers=headers, params=params)


def _is_http_url(url: str) -> bool:
    """Return True when URL has an explicit http/https scheme."""
    parsed = urlsplit(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _request_and_assess(
    *,
    provider_slug: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    timeout_seconds: int = 8,
) -> ProviderKeyValidationResult:
    """Perform probe request and normalize outcome into validation result."""
    try:
        response = _http_get(
            url,
            headers=headers,
            params=params,
            timeout_seconds=timeout_seconds,
        )
        return _assess_response(provider_slug, response)
    except httpx.UnsupportedProtocol:
        return ProviderKeyValidationResult(
            valid=False,
            definitive=True,
            message=f"{provider_slug}: invalid API base URL '{url}'. Include http:// or https://.",
        )
    except httpx.TimeoutException:
        return ProviderKeyValidationResult(
            valid=True,
            definitive=False,
            message=f"{provider_slug}: validation timed out. Key saved with inconclusive validation.",
        )
    except httpx.RequestError as exc:
        return ProviderKeyValidationResult(
            valid=True,
            definitive=False,
            message=f"{provider_slug}: validation request failed ({exc.__class__.__name__}). "
            "Key saved with inconclusive validation.",
        )


def _assess_response(provider_slug: str, response: httpx.Response) -> ProviderKeyValidationResult:
    """Map HTTP status codes to validation success/failure semantics."""
    status_code = response.status_code

    if 200 <= status_code < 300:
        return ProviderKeyValidationResult(valid=True, definitive=True, message="Provider key validated successfully.")

    if status_code in {401, 403}:
        return ProviderKeyValidationResult(
            valid=False,
            definitive=True,
            message=f"{provider_slug}: key rejected by provider (HTTP {status_code}).",
        )

    if status_code == 404:
        if is_azure_provider_slug(provider_slug):
            return ProviderKeyValidationResult(
                valid=False,
                definitive=False,
                message=(
                    f"{provider_slug}: endpoint/config not found (HTTP 404). "
                    "Use a resource base URL (https://<resource>.openai.azure.com) "
                    "or an OpenAI-compatible base (.../openai/v1), "
                    "or Foundry inference base (...services.ai.azure.com/models)."
                ),
            )
        return ProviderKeyValidationResult(
            valid=False,
            definitive=False,
            message=f"{provider_slug}: endpoint/config not found (HTTP 404). Check API base/version.",
        )

    if status_code == 429:
        return ProviderKeyValidationResult(
            valid=True,
            definitive=False,
            message=f"{provider_slug}: validation rate-limited (HTTP 429). Key saved, but not fully confirmed.",
        )

    if status_code >= 500:
        return ProviderKeyValidationResult(
            valid=True,
            definitive=False,
            message=f"{provider_slug}: provider unavailable (HTTP {status_code}). Key saved with inconclusive validation.",
        )

    return ProviderKeyValidationResult(
        valid=False,
        definitive=False,
        message=f"{provider_slug}: validation failed (HTTP {status_code}). Check API base/version.",
    )


def validate_provider_api_key(
    *,
    provider_slug: str,
    api_key: str,
    api_base: str | None = None,
    api_version: str | None = None,
    timeout_seconds: int = 8,
) -> ProviderKeyValidationResult:
    """Validate provider key + endpoint settings with provider-specific probes."""
    normalized = ensure_supported_provider_slug(provider_slug)
    incoming_key = (api_key or "").strip()
    if not incoming_key:
        return ProviderKeyValidationResult(valid=False, definitive=True, message="API key is empty.")

    resolved_base, resolved_version = resolve_provider_endpoint_options(
        normalized,
        api_base=api_base,
        api_version=api_version,
        use_platform_defaults=False,
    )

    if resolved_base and not _is_http_url(resolved_base):
        return ProviderKeyValidationResult(
            valid=False,
            definitive=True,
            message=f"{normalized}: API base must include http:// or https:// (got '{resolved_base}').",
        )

    if normalized == "openai":
        base = resolved_base or "https://api.openai.com/v1"
        base = base.rstrip("/")
        return _request_and_assess(
            provider_slug=normalized,
            url=f"{base}/models",
            headers={"Authorization": f"Bearer {incoming_key}"},
            timeout_seconds=timeout_seconds,
        )

    if is_azure_provider_slug(normalized):
        if not resolved_base:
            return ProviderKeyValidationResult(
                valid=False,
                definitive=True,
                message=f"{normalized}: API base URL is required in the connection form.",
            )

        def _azure_resource_base(url: str) -> str:
            parsed = urlsplit(url)
            path = parsed.path or ""
            for marker in ("/openai/v1", "/openai/deployments", "/openai"):
                if marker in path:
                    path = path.split(marker, 1)[0]
                    break
            return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))

        base = resolved_base.rstrip("/")
        resource_base = _azure_resource_base(base)
        legacy_params = {"api-version": resolved_version or "2024-10-21"}
        mode = azure_provider_mode(normalized) or "auto"

        if mode == "deployment":
            return _request_and_assess(
                provider_slug=normalized,
                url=f"{resource_base}/openai/deployments",
                headers={"api-key": incoming_key},
                params=legacy_params,
                timeout_seconds=timeout_seconds,
            )

        if mode == "openai_v1":
            primary = _request_and_assess(
                provider_slug=normalized,
                url=f"{base}/models",
                headers={"api-key": incoming_key},
                timeout_seconds=timeout_seconds,
            )
            if primary.valid or primary.definitive:
                return primary
            bearer_probe = _request_and_assess(
                provider_slug=normalized,
                url=f"{base}/models",
                headers={"Authorization": f"Bearer {incoming_key}"},
                timeout_seconds=timeout_seconds,
            )
            if bearer_probe.valid or bearer_probe.definitive:
                return bearer_probe
            return primary

        if mode == "foundry":
            primary = _request_and_assess(
                provider_slug=normalized,
                url=base,
                headers={"api-key": incoming_key},
                timeout_seconds=timeout_seconds,
            )
            if primary.valid or primary.definitive:
                return primary
            bearer_probe = _request_and_assess(
                provider_slug=normalized,
                url=base,
                headers={"Authorization": f"Bearer {incoming_key}"},
                timeout_seconds=timeout_seconds,
            )
            if bearer_probe.valid or bearer_probe.definitive:
                return bearer_probe
            return primary

        # auto mode
        if "/openai/v1" in base:
            primary = _request_and_assess(
                provider_slug=normalized,
                url=f"{base}/models",
                headers={"api-key": incoming_key},
                timeout_seconds=timeout_seconds,
            )
            if primary.valid or primary.definitive:
                return primary

            bearer_probe = _request_and_assess(
                provider_slug=normalized,
                url=f"{base}/models",
                headers={"Authorization": f"Bearer {incoming_key}"},
                timeout_seconds=timeout_seconds,
            )
            if bearer_probe.valid or bearer_probe.definitive:
                return bearer_probe

            if resource_base and resource_base != base:
                legacy_probe = _request_and_assess(
                    provider_slug=normalized,
                    url=f"{resource_base}/openai/deployments",
                    headers={"api-key": incoming_key},
                    params=legacy_params,
                    timeout_seconds=timeout_seconds,
                )
                if legacy_probe.valid or legacy_probe.definitive:
                    return legacy_probe
            return primary

        legacy_probe = _request_and_assess(
            provider_slug=normalized,
            url=f"{resource_base}/openai/deployments",
            headers={"api-key": incoming_key},
            params=legacy_params,
            timeout_seconds=timeout_seconds,
        )
        if legacy_probe.valid or legacy_probe.definitive:
            return legacy_probe

        v1_base = f"{resource_base}/openai/v1"
        v1_probe = _request_and_assess(
            provider_slug=normalized,
            url=f"{v1_base}/models",
            headers={"api-key": incoming_key},
            timeout_seconds=timeout_seconds,
        )
        if v1_probe.valid or v1_probe.definitive:
            return v1_probe

        bearer_probe = _request_and_assess(
            provider_slug=normalized,
            url=f"{v1_base}/models",
            headers={"Authorization": f"Bearer {incoming_key}"},
            timeout_seconds=timeout_seconds,
        )
        if bearer_probe.valid or bearer_probe.definitive:
            return bearer_probe

        return legacy_probe

    return ProviderKeyValidationResult(valid=False, definitive=True, message=f"Unsupported provider '{provider_slug}'.")
