"""Provider runtime adapter for OpenAI-compatible completion calls.

The worker and batch services call into this module to execute one model route
while abstracting provider mode differences (OpenAI vs Azure variants).
"""

from __future__ import annotations

import re
from typing import Any

from openai import AzureOpenAI, OpenAI

from app.core.provider_profiles import azure_provider_mode, is_azure_provider_slug
from app.services.providers import resolve_provider_endpoint_options


def _extract_openai_output_text(response: Any) -> str:
    """Extract text from Responses API object payload."""
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text

    chunks: list[str] = []
    output = getattr(response, "output", None) or []
    for item in output:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(text)
    return "\n".join(chunks)


def _extract_chat_completion_text(response: Any) -> str:
    """Extract text from Chat Completions response payload."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    first = choices[0]
    message = getattr(first, "message", None)
    if message is None:
        return ""

    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks)
    return str(content or "")


def _sanitize_openai_params(params: dict[str, Any]) -> dict[str, Any]:
    """Keep only known-safe Responses API parameters."""
    allowed_keys = {
        "temperature",
        "max_output_tokens",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "reasoning",
        "metadata",
    }
    return {k: v for k, v in (params or {}).items() if k in allowed_keys}


def _sanitize_chat_params(params: dict[str, Any]) -> dict[str, Any]:
    """Map generic params to Chat Completions-compatible parameters."""
    source = params or {}
    cleaned: dict[str, Any] = {}

    for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty", "seed", "metadata"):
        if key in source:
            cleaned[key] = source[key]

    max_tokens = source.get("max_tokens", source.get("max_output_tokens"))
    if max_tokens is not None:
        cleaned["max_tokens"] = max_tokens

    return cleaned


def _extract_unsupported_parameter(error: Exception) -> str | None:
    """Parse unsupported parameter name from provider error text."""
    message = str(error)
    if not message:
        return None

    match = re.search(r"Unsupported parameter:\s*['\"]([^'\"]+)['\"]", message)
    if match:
        return match.group(1)
    return None


def _normalize_param_alias(param: str, available: dict[str, Any]) -> str | None:
    """Normalize parameter aliases to actual request payload keys."""
    if param in available:
        return param

    alias_map = {
        "max_tokens": "max_output_tokens",
        "max_output_tokens": "max_tokens",
    }
    alias = alias_map.get(param)
    if alias and alias in available:
        return alias

    if param.startswith("reasoning.") and "reasoning" in available:
        return "reasoning"

    return None


def _drop_known_incompatible_response_params(model: str, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Drop parameters known to be incompatible for selected model family."""
    cleaned = dict(params)
    removed: list[str] = []
    model_name = (model or "").lower().strip()

    # GPT-5 family rejects temperature/top_p unless using gpt-5.1 with reasoning.effort=none.
    if model_name.startswith("gpt-5"):
        allow_sampling = False
        if model_name.startswith("gpt-5.1"):
            reasoning = cleaned.get("reasoning")
            if isinstance(reasoning, dict) and str(reasoning.get("effort", "")).lower() == "none":
                allow_sampling = True
        if not allow_sampling:
            for key in ("temperature", "top_p"):
                if key in cleaned:
                    cleaned.pop(key, None)
                    removed.append(key)

    return cleaned, removed


def sanitize_responses_params_for_model(model: str, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Public helper used to pre-sanitize Responses API parameter payload."""
    return _drop_known_incompatible_response_params(model, _sanitize_openai_params(params))


def _messages_from_payload(system_prompt: str, input_payload: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build chat message array from raw string or chat-style input payload."""
    messages: list[dict[str, Any]] = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})

    if isinstance(input_payload, str):
        messages.append({"role": "user", "content": input_payload})
        return messages

    for item in input_payload:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        content = item.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if isinstance(content, list):
            normalized_parts: list[dict[str, Any]] = []
            for part in content:
                if isinstance(part, dict):
                    normalized_parts.append(part)
            if normalized_parts:
                messages.append({"role": role, "content": normalized_parts})
    return messages


def _extract_usage(response: Any) -> dict[str, Any] | None:
    """Extract usage metadata from SDK response objects."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    model_dump = getattr(usage, "model_dump", None)
    if callable(model_dump):
        return model_dump()

    to_dict = getattr(usage, "to_dict", None)
    if callable(to_dict):
        return to_dict()

    if isinstance(usage, dict):
        return usage

    return None


def extract_response_text_from_dict(payload: dict[str, Any]) -> str:
    """Extract text when response is already converted to dict form."""
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    chunks: list[str] = []
    output = payload.get("output")
    if not isinstance(output, list):
        return ""

    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
    return "\n".join(chunks)


def extract_usage_from_dict(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract usage metadata when response is in dict form."""
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    return None


def _client_has_responses(client: Any) -> bool:
    """Return True when SDK client supports Responses API."""
    responses = getattr(client, "responses", None)
    return responses is not None and hasattr(responses, "create")


def _run_responses_create(
    *,
    client: Any,
    model: str,
    system_prompt: str,
    input_payload: str | list[dict[str, Any]],
    params: dict[str, Any],
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Run one Responses API call with automatic unsupported-param fallback."""
    request_params, removed = _drop_known_incompatible_response_params(
        model,
        _sanitize_openai_params(params),
    )

    max_attempts = max(1, len(request_params) + 1)
    last_error: Exception | None = None

    for _ in range(max_attempts):
        try:
            response = client.responses.create(
                model=model,
                instructions=system_prompt,
                input=input_payload,
                **request_params,
            )
            usage = _extract_usage(response) or {}
            if removed:
                usage = dict(usage)
                usage["dropped_unsupported_params"] = removed
            return _extract_openai_output_text(response), getattr(response, "id", None), usage
        except Exception as exc:  # noqa: BLE001
            unsupported = _extract_unsupported_parameter(exc)
            normalized = _normalize_param_alias(unsupported, request_params) if unsupported else None
            if not normalized:
                raise
            request_params.pop(normalized, None)
            removed.append(normalized)
            last_error = exc

    if last_error is not None:
        raise last_error
    raise RuntimeError("Provider call failed without a specific error.")


def _run_chat_completions_create(
    *,
    client: Any,
    model: str,
    system_prompt: str,
    input_payload: str | list[dict[str, Any]],
    params: dict[str, Any],
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Run one Chat Completions call with automatic unsupported-param fallback."""
    request_params = _sanitize_chat_params(params)
    removed: list[str] = []
    max_attempts = max(1, len(request_params) + 1)
    last_error: Exception | None = None

    for _ in range(max_attempts):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=_messages_from_payload(system_prompt, input_payload),
                **request_params,
            )
            usage = _extract_usage(response) or {}
            if removed:
                usage = dict(usage)
                usage["dropped_unsupported_params"] = removed
            return _extract_chat_completion_text(response), getattr(response, "id", None), usage
        except Exception as exc:  # noqa: BLE001
            unsupported = _extract_unsupported_parameter(exc)
            normalized = _normalize_param_alias(unsupported, request_params) if unsupported else None
            if not normalized:
                raise
            request_params.pop(normalized, None)
            removed.append(normalized)
            last_error = exc

    if last_error is not None:
        raise last_error
    raise RuntimeError("Provider call failed without a specific error.")


def _build_client(
    *,
    provider_slug: str,
    api_key: str,
    api_base: str | None,
    api_version: str | None,
    timeout_seconds: int | None,
    max_retries: int | None,
) -> Any:
    """Create an OpenAI SDK client matching provider endpoint mode."""
    resolved_base, resolved_version = resolve_provider_endpoint_options(
        provider_slug,
        api_base=api_base,
        api_version=api_version,
    )
    common_kwargs: dict[str, Any] = {"api_key": api_key}
    if timeout_seconds and timeout_seconds > 0:
        common_kwargs["timeout"] = timeout_seconds
    if max_retries is not None and max_retries >= 0:
        common_kwargs["max_retries"] = max_retries

    if provider_slug == "openai":
        if resolved_base:
            common_kwargs["base_url"] = resolved_base
        return OpenAI(**common_kwargs)

    if is_azure_provider_slug(provider_slug):
        mode = azure_provider_mode(provider_slug) or "auto"
        normalized_base = (resolved_base or "").strip().rstrip("/")

        if mode in {"openai_v1", "foundry"}:
            if not normalized_base:
                raise RuntimeError(f"Provider '{provider_slug}' requires api_base.")
            common_kwargs["base_url"] = normalized_base
            return OpenAI(**common_kwargs)

        if mode == "deployment":
            if not normalized_base:
                raise RuntimeError(
                    f"Provider '{provider_slug}' requires api_base (for example https://<resource>.openai.azure.com)."
                )
            azure_kwargs = dict(common_kwargs)
            azure_kwargs["azure_endpoint"] = normalized_base
            azure_kwargs["api_version"] = resolved_version or "2024-10-21"
            return AzureOpenAI(**azure_kwargs)

        # auto mode
        if normalized_base and ("/openai/v1" in normalized_base or normalized_base.endswith("/models")):
            common_kwargs["base_url"] = normalized_base
            return OpenAI(**common_kwargs)

        if not normalized_base:
            raise RuntimeError(
                f"Provider '{provider_slug}' requires api_base. "
                "Set AZURE_OPENAI_BASE_URL (recommended) or AZURE_OPENAI_ENDPOINT."
            )

        azure_kwargs = dict(common_kwargs)
        azure_kwargs["azure_endpoint"] = normalized_base
        azure_kwargs["api_version"] = resolved_version or "2024-10-21"
        return AzureOpenAI(**azure_kwargs)

    raise RuntimeError(f"Unsupported provider '{provider_slug}'")


def build_provider_client(
    *,
    provider_slug: str,
    api_key: str,
    api_base: str | None,
    api_version: str | None,
    timeout_seconds: int | None,
    max_retries: int | None,
) -> Any:
    """Public client factory used by runtime and batch adapters."""
    return _build_client(
        provider_slug=provider_slug,
        api_key=api_key,
        api_base=api_base,
        api_version=api_version,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )


def run_provider_completion(
    *,
    provider_slug: str,
    model: str,
    api_key: str | None,
    api_base: str | None,
    api_version: str | None,
    system_prompt: str,
    input_payload: str | list[dict[str, Any]],
    params: dict[str, Any],
    timeout_seconds: int | None = None,
    max_retries: int | None = None,
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Execute one completion attempt and return text/result metadata."""
    if not api_key:
        raise RuntimeError(f"No API key available for provider '{provider_slug}'")

    client = build_provider_client(
        provider_slug=provider_slug,
        api_key=api_key,
        api_base=api_base,
        api_version=api_version,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )

    # Newer SDK versions expose Responses API directly; older versions may only
    # expose Chat Completions. We support both paths for runtime compatibility.
    if _client_has_responses(client):
        return _run_responses_create(
            client=client,
            model=model,
            system_prompt=system_prompt,
            input_payload=input_payload,
            params=params,
        )

    return _run_chat_completions_create(
        client=client,
        model=model,
        system_prompt=system_prompt,
        input_payload=input_payload,
        params=params,
    )
