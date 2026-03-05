from types import SimpleNamespace

from app.services import targets as target_service


class _DummyDB:
    def add(self, *_args, **_kwargs):
        return None

    def commit(self):
        return None

    def refresh(self, *_args, **_kwargs):
        return None


def test_verify_target_success(monkeypatch) -> None:
    target = SimpleNamespace(
        tenant_id="tenant_1",
        provider_slug="openai",
        model_identifier="gpt-5-nano",
        capability_profile="responses_chat",
        params_json={},
        is_active=True,
        is_verified=False,
        last_verified_at=None,
        last_verification_error="bad",
    )

    monkeypatch.setattr(
        target_service,
        "resolve_provider_credentials",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_slug="openai",
            api_key="sk-test",
            api_base=None,
            api_version=None,
        ),
    )
    monkeypatch.setattr(
        target_service,
        "run_provider_completion",
        lambda *_args, **_kwargs: ("ok", "resp_1", {"input_tokens": 1, "output_tokens": 1}),
    )

    ok, message = target_service.verify_target(_DummyDB(), target)
    assert ok is True
    assert "successfully" in message.lower()
    assert target.is_verified is True
    assert target.last_verification_error is None
    assert target.last_verified_at is not None


def test_verify_target_failure(monkeypatch) -> None:
    target = SimpleNamespace(
        tenant_id="tenant_1",
        provider_slug="openai",
        model_identifier="gpt-5-nano",
        capability_profile="responses_chat",
        params_json={},
        is_active=True,
        is_verified=True,
        last_verified_at=None,
        last_verification_error=None,
    )

    monkeypatch.setattr(
        target_service,
        "resolve_provider_credentials",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_slug="openai",
            api_key="sk-test",
            api_base=None,
            api_version=None,
        ),
    )

    def _raise(*_args, **_kwargs):
        raise RuntimeError("bad upstream")

    monkeypatch.setattr(target_service, "run_provider_completion", _raise)

    ok, message = target_service.verify_target(_DummyDB(), target)
    assert ok is False
    assert "failed" in message.lower()
    assert target.is_verified is False
    assert target.last_verified_at is not None
    assert target.last_verification_error == "bad upstream"
