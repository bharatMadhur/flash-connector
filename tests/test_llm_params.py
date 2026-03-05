from __future__ import annotations

from app.services import llm


class _Usage:
    def model_dump(self) -> dict[str, int]:
        return {"input_tokens": 1, "output_tokens": 1}


class _Response:
    output_text = "ok"
    id = "resp_123"
    usage = _Usage()


class _ResponsesApi:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if "temperature" in kwargs:
            raise RuntimeError(
                "Error code: 400 - {'error': {'message': \"Unsupported parameter: 'temperature' is not supported with this model.\"}}"
            )
        return _Response()


class _Client:
    def __init__(self) -> None:
        self.responses = _ResponsesApi()


def test_gpt5_drops_temperature_before_request(monkeypatch) -> None:
    client = _Client()
    monkeypatch.setattr(llm, "_build_client", lambda **_kwargs: client)

    text, response_id, usage = llm.run_provider_completion(
        provider_slug="openai",
        model="gpt-5",
        api_key="sk-test",
        api_base=None,
        api_version=None,
        system_prompt="test",
        input_payload="hello",
        params={"temperature": 0.2, "max_output_tokens": 16},
    )

    assert text == "ok"
    assert response_id == "resp_123"
    assert len(client.responses.calls) == 1
    assert "temperature" not in client.responses.calls[0]
    assert usage is not None
    assert usage["dropped_unsupported_params"] == ["temperature"]


def test_non_gpt5_retries_after_unsupported_parameter(monkeypatch) -> None:
    client = _Client()
    monkeypatch.setattr(llm, "_build_client", lambda **_kwargs: client)

    text, response_id, usage = llm.run_provider_completion(
        provider_slug="openai",
        model="gpt-4.1",
        api_key="sk-test",
        api_base=None,
        api_version=None,
        system_prompt="test",
        input_payload="hello",
        params={"temperature": 0.2, "max_output_tokens": 16},
    )

    assert text == "ok"
    assert response_id == "resp_123"
    assert len(client.responses.calls) == 2
    assert "temperature" in client.responses.calls[0]
    assert "temperature" not in client.responses.calls[1]
    assert usage is not None
    assert usage["dropped_unsupported_params"] == ["temperature"]
