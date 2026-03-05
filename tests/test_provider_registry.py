from app.core.provider_catalog import (
    get_model_parameters,
    list_equivalent_models,
    list_provider_models,
)
from app.core.provider_registry import get_model_spec, get_provider_spec, list_models_for_provider, list_provider_specs


def test_provider_registry_has_openai_and_azure() -> None:
    providers = list_provider_specs()
    slugs = [item.slug for item in providers]
    assert slugs == [
        "openai",
        "azure_openai",
        "azure_openai_v1",
        "azure_openai_deployment",
        "azure_ai_foundry",
    ]


def test_provider_registry_models_exist_for_each_provider() -> None:
    for provider in list_provider_specs():
        models = list_models_for_provider(provider.slug)
        assert len(models) > 0


def test_gpt5_temperature_metadata_is_marked_unsupported() -> None:
    model = get_model_spec("openai", "gpt-5")
    assert model is not None
    temperature = next((item for item in model.parameters if item.key == "temperature"), None)
    assert temperature is not None
    assert temperature.supported is False


def test_provider_catalog_helpers_expose_model_metadata() -> None:
    models = list_provider_models("openai")
    assert "gpt-5" in models
    assert "gpt-5.2" in models

    params = get_model_parameters("openai", "gpt-4.1")
    assert any(item["key"] == "temperature" and item["supported"] for item in params)


def test_azure_profiles_inherit_model_catalog() -> None:
    models = list_provider_models("azure_openai_v1")
    assert "gpt-4.1-mini" in models


def test_equivalent_models_mapping_exists() -> None:
    eq = list_equivalent_models("openai", "gpt-5-mini")
    assert any(item["provider"] == "azure_openai" and item["model"] == "gpt-5-mini" for item in eq)
    assert any(item["provider"] == "azure_openai_v1" and item["model"] == "gpt-5-mini" for item in eq)


def test_latest_flagship_equivalence_mapping_exists() -> None:
    eq = list_equivalent_models("openai", "gpt-5.2")
    assert any(item["provider"] == "azure_openai" and item["model"] == "gpt-5.2" for item in eq)
    assert any(item["provider"] == "azure_openai_deployment" and item["model"] == "gpt-5.2" for item in eq)


def test_provider_spec_contains_connection_fields() -> None:
    provider = get_provider_spec("azure_openai")
    assert provider is not None
    keys = [field.key for field in provider.connection_fields]
    assert "api_key" in keys
    assert "api_base" in keys

    deployment_provider = get_provider_spec("azure_openai_deployment")
    assert deployment_provider is not None
    deployment_keys = [field.key for field in deployment_provider.connection_fields]
    assert "api_version" in deployment_keys
