from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_DIR = REPO_ROOT / "providers"


def _load_yaml(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"{path} must contain a top-level YAML mapping")
    return payload


def test_provider_yaml_shape_is_strict() -> None:
    allowed_top_level = {
        "slug",
        "name",
        "aliases",
        "logo_path",
        "models_from",
        "platform_key_env",
        "requires_api_key",
        "docs",
        "connection_fields",
        "default_model",
        "recommended_models",
    }
    required_top_level = {
        "slug",
        "name",
        "docs",
        "connection_fields",
        "default_model",
        "recommended_models",
        "requires_api_key",
    }
    allowed_field_keys = {"key", "label", "type", "required", "placeholder", "description"}

    provider_dirs = sorted(path for path in PROVIDERS_DIR.iterdir() if path.is_dir() and (path / "provider.yaml").exists())
    assert provider_dirs, "No providers found"

    for provider_dir in provider_dirs:
        provider_yaml = provider_dir / "provider.yaml"
        payload = _load_yaml(provider_yaml)

        unknown = set(payload) - allowed_top_level
        assert not unknown, f"{provider_yaml} has unknown keys: {sorted(unknown)}"
        missing = required_top_level - set(payload)
        assert not missing, f"{provider_yaml} is missing required keys: {sorted(missing)}"

        assert isinstance(payload["slug"], str) and payload["slug"].strip()
        assert isinstance(payload["name"], str) and payload["name"].strip()
        assert isinstance(payload["default_model"], str) and payload["default_model"].strip()
        assert isinstance(payload["requires_api_key"], bool)

        docs = payload["docs"]
        assert isinstance(docs, dict), f"{provider_yaml}: docs must be a mapping"
        assert isinstance(docs.get("api"), str) and docs.get("api", "").strip(), f"{provider_yaml}: docs.api is required"

        aliases = payload.get("aliases", [])
        assert isinstance(aliases, list), f"{provider_yaml}: aliases must be a list"
        for alias in aliases:
            assert isinstance(alias, str) and alias.strip(), f"{provider_yaml}: aliases must be non-empty strings"

        recommended_models = payload["recommended_models"]
        assert isinstance(recommended_models, list) and recommended_models, f"{provider_yaml}: recommended_models required"
        assert payload["default_model"] in recommended_models, f"{provider_yaml}: default_model must exist in recommended_models"

        models_from = payload.get("models_from")
        if models_from is not None:
            assert isinstance(models_from, str) and models_from.strip(), f"{provider_yaml}: models_from must be non-empty string"
            assert (PROVIDERS_DIR / models_from / "provider.yaml").exists(), (
                f"{provider_yaml}: models_from target '{models_from}' not found"
            )

        connection_fields = payload["connection_fields"]
        assert isinstance(connection_fields, list) and connection_fields, f"{provider_yaml}: connection_fields required"
        for item in connection_fields:
            assert isinstance(item, dict), f"{provider_yaml}: each connection_field must be a mapping"
            unknown_field_keys = set(item) - allowed_field_keys
            assert not unknown_field_keys, (
                f"{provider_yaml}: connection field has unknown keys: {sorted(unknown_field_keys)}"
            )
            for key in ("key", "label", "type", "required"):
                assert key in item, f"{provider_yaml}: connection field missing '{key}'"
            assert isinstance(item["key"], str) and item["key"].strip()
            assert isinstance(item["label"], str) and item["label"].strip()
            assert isinstance(item["type"], str) and item["type"].strip()
            assert isinstance(item["required"], bool)


def test_model_yaml_shape_is_strict() -> None:
    allowed_model_keys = {
        "model",
        "display_name",
        "family",
        "category",
        "supports_realtime",
        "supports_vision",
        "supports_tools",
        "notes",
        "parameters",
    }
    allowed_param_keys = {
        "supported",
        "type",
        "min",
        "max",
        "default",
        "values",
        "description",
        "increase_effect",
        "decrease_effect",
    }

    model_files = sorted(PROVIDERS_DIR.glob("*/models/*.yaml"))
    assert model_files, "No provider model YAML files found"

    for model_yaml in model_files:
        payload = _load_yaml(model_yaml)

        unknown = set(payload) - allowed_model_keys
        assert not unknown, f"{model_yaml} has unknown keys: {sorted(unknown)}"
        for key in ("model", "display_name", "supports_realtime", "supports_vision", "supports_tools", "parameters"):
            assert key in payload, f"{model_yaml} missing required key '{key}'"

        assert isinstance(payload["model"], str) and payload["model"].strip()
        assert isinstance(payload["display_name"], str) and payload["display_name"].strip()
        assert isinstance(payload["supports_realtime"], bool)
        assert isinstance(payload["supports_vision"], bool)
        assert isinstance(payload["supports_tools"], bool)

        parameters = payload["parameters"]
        assert isinstance(parameters, dict), f"{model_yaml}: parameters must be a mapping"
        for param_name, spec in parameters.items():
            assert isinstance(param_name, str) and param_name.strip()
            assert isinstance(spec, dict), f"{model_yaml}: parameter '{param_name}' must be a mapping"
            unknown_param_keys = set(spec) - allowed_param_keys
            assert not unknown_param_keys, (
                f"{model_yaml}: parameter '{param_name}' has unknown keys: {sorted(unknown_param_keys)}"
            )
            assert "supported" in spec and isinstance(spec["supported"], bool), (
                f"{model_yaml}: parameter '{param_name}' must define boolean 'supported'"
            )
            if "type" in spec:
                assert isinstance(spec["type"], str) and spec["type"].strip(), (
                    f"{model_yaml}: parameter '{param_name}' type must be a string"
                )
            if spec.get("type") == "enum":
                values = spec.get("values")
                assert isinstance(values, list) and values, (
                    f"{model_yaml}: enum parameter '{param_name}' must define non-empty values list"
                )
