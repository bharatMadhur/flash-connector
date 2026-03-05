"""Provider profile helpers.

This module centralizes normalized provider-family behavior flags that are reused
across validation/runtime code. Today this is mostly Azure mode handling.
"""

from __future__ import annotations

from typing import Literal


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower().replace("-", "_").replace(" ", "_")


AzureProviderMode = Literal["auto", "deployment", "openai_v1", "foundry"]

AZURE_PROVIDER_MODES: dict[str, AzureProviderMode] = {
    "azure_openai": "auto",
    "azure_openai_deployment": "deployment",
    "azure_openai_v1": "openai_v1",
    "azure_ai_foundry": "foundry",
}


def is_azure_provider_slug(provider_slug: str | None) -> bool:
    """Return True when the slug belongs to one of the supported Azure families."""
    return _normalize(provider_slug) in AZURE_PROVIDER_MODES


def azure_provider_mode(provider_slug: str | None) -> AzureProviderMode | None:
    """Resolve the Azure transport mode from a provider slug."""
    return AZURE_PROVIDER_MODES.get(_normalize(provider_slug))


def provider_sort_priority(provider_slug: str | None) -> int:
    """Stable sort order for provider cards/menus in the UI."""
    normalized = _normalize(provider_slug)
    priorities = {
        "openai": 0,
        "azure_openai": 1,
        "azure_openai_v1": 2,
        "azure_openai_deployment": 3,
        "azure_ai_foundry": 4,
    }
    return priorities.get(normalized, 99)
