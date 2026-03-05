from app.services.providers import resolve_provider_endpoint_options


def test_resolve_azure_full_chat_endpoint_normalizes_to_resource_and_extracts_version() -> None:
    base, version = resolve_provider_endpoint_options(
        "azure_openai",
        api_base=(
            "https://llm-inference.openai.azure.com/openai/deployments/"
            "gpt-4.1-mini-dev-env/chat/completions?api-version=2025-01-01-preview"
        ),
        api_version=None,
        use_platform_defaults=False,
    )
    assert base == "https://llm-inference.openai.azure.com/openai/v1"
    assert version is None


def test_resolve_azure_openai_v1_clears_api_version() -> None:
    base, version = resolve_provider_endpoint_options(
        "azure_openai",
        api_base="https://llm-inference.openai.azure.com/openai/v1?api-version=2025-01-01-preview",
        api_version=None,
        use_platform_defaults=False,
    )
    assert base == "https://llm-inference.openai.azure.com/openai/v1"
    assert version is None


def test_resolve_azure_resource_root_keeps_root() -> None:
    base, version = resolve_provider_endpoint_options(
        "azure_openai",
        api_base="https://llm-inference.openai.azure.com",
        api_version="2025-01-01-preview",
        use_platform_defaults=False,
    )
    assert base == "https://llm-inference.openai.azure.com/openai/v1"
    assert version is None


def test_resolve_standard_azure_resource_root_keeps_legacy_mode() -> None:
    base, version = resolve_provider_endpoint_options(
        "azure_openai",
        api_base="https://my-resource.openai.azure.com",
        api_version="2025-01-01-preview",
        use_platform_defaults=False,
    )
    assert base == "https://my-resource.openai.azure.com"
    assert version == "2025-01-01-preview"


def test_resolve_azure_v1_profile_forces_openai_v1_path() -> None:
    base, version = resolve_provider_endpoint_options(
        "azure_openai_v1",
        api_base="https://my-resource.openai.azure.com",
        api_version="2025-01-01-preview",
        use_platform_defaults=False,
    )
    assert base == "https://my-resource.openai.azure.com/openai/v1"
    assert version is None


def test_resolve_azure_deployment_profile_forces_resource_root_and_default_version() -> None:
    base, version = resolve_provider_endpoint_options(
        "azure_openai_deployment",
        api_base=(
            "https://my-resource.openai.azure.com/openai/deployments/"
            "my-deployment/chat/completions?api-version=2025-01-01-preview"
        ),
        api_version=None,
        use_platform_defaults=False,
    )
    assert base == "https://my-resource.openai.azure.com"
    assert version == "2025-01-01-preview"


def test_resolve_azure_ai_foundry_profile_forces_models_path() -> None:
    base, version = resolve_provider_endpoint_options(
        "azure_ai_foundry",
        api_base="https://my-project.services.ai.azure.com",
        api_version=None,
        use_platform_defaults=False,
    )
    assert base == "https://my-project.services.ai.azure.com/models"
    assert version is None
