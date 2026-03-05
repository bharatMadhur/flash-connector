from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.db import get_db
from app.dependencies import get_api_key_context
from app.main import app
from app.routers import public as public_router
from app.services.api_keys import ApiKeyContext
from app.schemas.api_keys import ApiKeyScopes


def _override_db():
    yield None


def _override_api_context() -> ApiKeyContext:
    return ApiKeyContext(
        api_key_id="key_1",
        tenant_id="tenant_1",
        scopes=ApiKeyScopes(all=True),
        rate_limit_per_min=60,
        monthly_quota=1000,
    )


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def enqueue(self, fn: str, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls.append((fn, args))
        return None


def test_submit_provider_batch_success(monkeypatch) -> None:
    fake_queue = _FakeQueue()

    monkeypatch.setattr(public_router, "key_allows_endpoint", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(public_router, "enforce_limits", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(public_router, "get_tenant_endpoint", lambda *_args, **_kwargs: SimpleNamespace(id="ep_1"))
    monkeypatch.setattr(
        public_router,
        "get_active_version",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="ver_1",
            provider="openai",
            model="gpt-5-mini",
            target_id=None,
            params_json={},
        ),
    )
    monkeypatch.setattr(
        public_router,
        "create_provider_batch_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="pbr_1",
            status="queued",
            provider_slug="openai",
            model_used="gpt-5-mini",
            total_jobs=2,
        ),
    )
    monkeypatch.setattr(public_router, "get_queue", lambda: fake_queue)

    app.dependency_overrides[get_api_key_context] = _override_api_context
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    try:
        response = client.post(
            "/v1/endpoints/ep_1/batches",
            headers={"x-api-key": "fc_test"},
            json={
                "items": [{"input": "a"}, {"input": "b"}],
                "metadata": {"service_tier": "flex"},
                "batch_name": "smoke",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["batch_id"] == "pbr_1"
    assert fake_queue.calls
    assert fake_queue.calls[0][0] == "app.tasks.submit_provider_batch_run"


def test_get_provider_batch(monkeypatch) -> None:
    monkeypatch.setattr(public_router, "key_allows_endpoint", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        public_router,
        "get_provider_batch_for_tenant",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="pbr_1",
            tenant_id="tenant_1",
            endpoint_id="ep_1",
            endpoint_version_id="ver_1",
            provider_slug="openai",
            provider_config_id=None,
            model_used="gpt-5-mini",
            status="queued",
            completion_window="24h",
            provider_batch_id=None,
            input_file_id=None,
            output_file_id=None,
            error_file_id=None,
            request_json={},
            result_json=None,
            error=None,
            total_jobs=2,
            completed_jobs=0,
            failed_jobs=0,
            canceled_jobs=0,
            cancel_requested=False,
            created_by_user_id=None,
            created_at="2026-02-27T12:00:00+00:00",
            started_at=None,
            finished_at=None,
            last_polled_at=None,
            next_poll_at=None,
            updated_at="2026-02-27T12:00:00+00:00",
        ),
    )
    monkeypatch.setattr(public_router, "get_queue", lambda: _FakeQueue())

    app.dependency_overrides[get_api_key_context] = _override_api_context
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    try:
        response = client.get("/v1/batches/pbr_1", headers={"x-api-key": "fc_test"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["id"] == "pbr_1"


def test_cancel_provider_batch(monkeypatch) -> None:
    monkeypatch.setattr(public_router, "key_allows_endpoint", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        public_router,
        "get_provider_batch_for_tenant",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="pbr_1",
            endpoint_id="ep_1",
            provider_batch_id="batch_x",
            status="processing",
        ),
    )
    monkeypatch.setattr(
        public_router,
        "request_cancel_provider_batch_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="pbr_1",
            provider_batch_id="batch_x",
            status="canceling",
        ),
    )
    monkeypatch.setattr(public_router, "get_queue", lambda: _FakeQueue())

    app.dependency_overrides[get_api_key_context] = _override_api_context
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    try:
        response = client.post("/v1/batches/pbr_1/cancel", headers={"x-api-key": "fc_test"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"batch_id": "pbr_1", "status": "canceling"}
