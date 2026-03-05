import httpx

from app.services import provider_validation


def test_validate_openai_key_success(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_validation,
        "_http_get",
        lambda *args, **kwargs: httpx.Response(200),
    )
    result = provider_validation.validate_provider_api_key(provider_slug="openai", api_key="sk-test")
    assert result.valid is True
    assert result.definitive is True


def test_validate_openai_key_invalid(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_validation,
        "_http_get",
        lambda *args, **kwargs: httpx.Response(401),
    )
    result = provider_validation.validate_provider_api_key(provider_slug="openai", api_key="bad")
    assert result.valid is False
    assert result.definitive is True


def test_validate_azure_requires_base() -> None:
    result = provider_validation.validate_provider_api_key(provider_slug="azure_openai", api_key="abc")
    assert result.valid is False
    assert result.definitive is True


def test_validate_openai_inconclusive_server_error(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_validation,
        "_http_get",
        lambda *args, **kwargs: httpx.Response(503),
    )
    result = provider_validation.validate_provider_api_key(provider_slug="openai", api_key="sk-test")
    assert result.valid is True
    assert result.definitive is False


def test_validate_openai_requires_http_scheme_for_api_base() -> None:
    result = provider_validation.validate_provider_api_key(
        provider_slug="openai",
        api_key="sk-test",
        api_base="api.openai.com/v1",
    )
    assert result.valid is False
    assert result.definitive is True
    assert "http://" in result.message or "https://" in result.message


def test_validate_azure_v1_falls_back_to_bearer_probe(monkeypatch) -> None:
    def fake_get(url: str, *, headers: dict[str, str], **kwargs) -> httpx.Response:
        if url.endswith("/models") and "api-key" in headers:
            return httpx.Response(404)
        if url.endswith("/models") and "Authorization" in headers:
            return httpx.Response(200)
        return httpx.Response(500)

    monkeypatch.setattr(provider_validation, "_http_get", fake_get)
    result = provider_validation.validate_provider_api_key(
        provider_slug="azure_openai",
        api_key="azure-key",
        api_base="https://example-resource.openai.azure.com/openai/v1",
    )
    assert result.valid is True
    assert result.definitive is True


def test_validate_azure_deployment_style_base_normalizes_to_resource(monkeypatch) -> None:
    seen_urls: list[str] = []

    def fake_get(url: str, *, headers: dict[str, str], **kwargs) -> httpx.Response:
        seen_urls.append(url)
        if url.endswith("/openai/deployments"):
            return httpx.Response(200)
        return httpx.Response(404)

    monkeypatch.setattr(provider_validation, "_http_get", fake_get)
    result = provider_validation.validate_provider_api_key(
        provider_slug="azure_openai",
        api_key="azure-key",
        api_base="https://example-resource.openai.azure.com/openai/deployments/my-deployment",
        api_version="2024-10-21",
    )
    assert result.valid is True
    assert result.definitive is True
    assert any(url.endswith("/openai/deployments") for url in seen_urls)


def test_validate_azure_v1_profile_uses_models_probe(monkeypatch) -> None:
    seen_urls: list[str] = []

    def fake_get(url: str, *, headers: dict[str, str], **kwargs) -> httpx.Response:
        seen_urls.append(url)
        if url.endswith("/openai/v1/models"):
            return httpx.Response(200)
        return httpx.Response(404)

    monkeypatch.setattr(provider_validation, "_http_get", fake_get)
    result = provider_validation.validate_provider_api_key(
        provider_slug="azure_openai_v1",
        api_key="azure-key",
        api_base="https://example-resource.openai.azure.com",
    )
    assert result.valid is True
    assert result.definitive is True
    assert any(url.endswith("/openai/v1/models") for url in seen_urls)


def test_validate_azure_deployment_profile_uses_deployments_probe(monkeypatch) -> None:
    seen_urls: list[str] = []

    def fake_get(url: str, *, headers: dict[str, str], **kwargs) -> httpx.Response:
        seen_urls.append(url)
        if url.endswith("/openai/deployments"):
            return httpx.Response(200)
        return httpx.Response(404)

    monkeypatch.setattr(provider_validation, "_http_get", fake_get)
    result = provider_validation.validate_provider_api_key(
        provider_slug="azure_openai_deployment",
        api_key="azure-key",
        api_base="https://example-resource.openai.azure.com/openai/v1",
        api_version="2025-01-01-preview",
    )
    assert result.valid is True
    assert result.definitive is True
    assert any(url.endswith("/openai/deployments") for url in seen_urls)
