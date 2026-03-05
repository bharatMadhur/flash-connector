import pytest

from app.core.provider_catalog import (
    ensure_supported_provider_slug,
    get_provider_catalog_item,
    list_provider_catalog,
    normalize_provider_slug,
)


def test_provider_catalog_has_expected_entries() -> None:
    providers = list_provider_catalog()
    assert [provider.slug for provider in providers] == [
        "openai",
        "azure_openai",
        "azure_openai_v1",
        "azure_openai_deployment",
        "azure_ai_foundry",
    ]


def test_provider_catalog_contains_expected_core_providers() -> None:
    assert get_provider_catalog_item("openai") is not None
    assert get_provider_catalog_item("azure_openai") is not None
    assert get_provider_catalog_item("azure_openai_v1") is not None
    assert get_provider_catalog_item("azure_openai_deployment") is not None
    assert get_provider_catalog_item("azure_ai_foundry") is not None


def test_normalize_provider_slug_handles_aliases() -> None:
    assert normalize_provider_slug("OpenAI") == "openai"
    assert normalize_provider_slug("azure") == "azure_openai"
    assert normalize_provider_slug("azure-openai") == "azure_openai"
    assert normalize_provider_slug("azure-openai-v1") == "azure_openai_v1"
    assert normalize_provider_slug("azure-openai-deployment") == "azure_openai_deployment"
    assert normalize_provider_slug("azure-foundry") == "azure_ai_foundry"


def test_ensure_supported_provider_slug_rejects_unsupported() -> None:
    with pytest.raises(ValueError):
        ensure_supported_provider_slug("openrouter")
