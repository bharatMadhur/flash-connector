from types import SimpleNamespace
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.core.db import get_db
from app.dependencies import get_api_key_context
from app.main import app
from app.models import JobStatus
from app.routers import public as public_router
from app.services.api_keys import ApiKeyContext
from app.schemas.api_keys import ApiKeyScopes


def _override_db():
    yield SimpleNamespace(
        expire_all=lambda: None,
        rollback=lambda: None,
        add=lambda *_args, **_kwargs: None,
        commit=lambda: None,
        refresh=lambda *_args, **_kwargs: None,
    )


def _override_api_context() -> ApiKeyContext:
    return ApiKeyContext(
        api_key_id="key_1",
        tenant_id="tenant_1",
        scopes=ApiKeyScopes(all=True),
        rate_limit_per_min=60,
        monthly_quota=1000,
    )


def _job_payload(job_id: str, status: str = "completed") -> dict:
    now = datetime.now(UTC)
    return {
        "id": job_id,
        "tenant_id": "tenant_1",
        "endpoint_id": "ep_1",
        "endpoint_version_id": "ver_1",
        "billing_mode": "byok",
        "reserved_cost_usd": 0.0,
        "request_api_key_id": "key_1",
        "idempotency_key": "idem-1",
        "subtenant_code": None,
        "status": status,
        "request_json": {"input": "hello"},
        "request_hash": "hash_1",
        "cache_hit": False,
        "cached_from_job_id": None,
        "result_text": "done" if status == "completed" else None,
        "error": None if status == "completed" else "failed",
        "usage_json": {"input_tokens": 3, "output_tokens": 1},
        "estimated_cost_usd": 0.000001,
        "provider_response_id": "resp_1",
        "provider_used": "openai",
        "model_used": "gpt-4.1-mini",
        "created_at": now,
        "started_at": now,
        "finished_at": now if status in {"completed", "failed", "canceled"} else None,
    }


def test_submit_job_reuses_existing_job_for_idempotency_key(monkeypatch) -> None:
    def _should_not_create(*_args, **_kwargs):
        raise AssertionError("create_job should not be called for an idempotent replay")

    def _should_not_enqueue():
        raise AssertionError("queue enqueue should not run for an idempotent replay")

    monkeypatch.setattr(public_router, "key_allows_endpoint", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(public_router, "enforce_limits", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(public_router, "get_tenant_endpoint", lambda *_args, **_kwargs: SimpleNamespace(id="ep_1"))
    monkeypatch.setattr(public_router, "get_active_version", lambda *_args, **_kwargs: SimpleNamespace(id="ver_1"))
    monkeypatch.setattr(
        public_router,
        "get_idempotent_job_for_key",
        lambda *_args, **_kwargs: SimpleNamespace(id="job_existing", status=JobStatus.queued),
    )
    monkeypatch.setattr(public_router, "create_job", _should_not_create)
    monkeypatch.setattr(public_router, "get_queue", _should_not_enqueue)

    app.dependency_overrides[get_api_key_context] = _override_api_context
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    try:
        response = client.post(
            "/v1/endpoints/ep_1/jobs",
            headers={"x-api-key": "fc_test", "Idempotency-Key": "idem-abc-1"},
            json={"input": "hello"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"job_id": "job_existing", "status": "queued"}


def test_submit_job_rejects_long_idempotency_key(monkeypatch) -> None:
    monkeypatch.setattr(public_router, "key_allows_endpoint", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(public_router, "enforce_limits", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(public_router, "get_tenant_endpoint", lambda *_args, **_kwargs: SimpleNamespace(id="ep_1"))
    monkeypatch.setattr(public_router, "get_active_version", lambda *_args, **_kwargs: SimpleNamespace(id="ver_1"))

    app.dependency_overrides[get_api_key_context] = _override_api_context
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    try:
        response = client.post(
            "/v1/endpoints/ep_1/jobs",
            headers={"x-api-key": "fc_test", "Idempotency-Key": "x" * 129},
            json={"input": "hello"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "Idempotency-Key" in response.json()["detail"]


def test_submit_response_executes_inline(monkeypatch) -> None:
    called: dict[str, str] = {}

    monkeypatch.setattr(public_router, "key_allows_endpoint", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(public_router, "enforce_limits", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(public_router, "get_tenant_endpoint", lambda *_args, **_kwargs: SimpleNamespace(id="ep_1"))
    monkeypatch.setattr(public_router, "get_active_version", lambda *_args, **_kwargs: SimpleNamespace(id="ver_1"))
    monkeypatch.setattr(
        public_router,
        "get_idempotent_job_for_key",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        public_router,
        "create_job",
        lambda *_args, **_kwargs: SimpleNamespace(id="job_inline", status=JobStatus.queued),
    )
    monkeypatch.setattr(public_router, "process_job", lambda job_id: called.setdefault("job_id", job_id))
    monkeypatch.setattr(
        public_router,
        "get_job_for_tenant",
        lambda *_args, **_kwargs: _job_payload("job_inline", status="completed"),
    )

    app.dependency_overrides[get_api_key_context] = _override_api_context
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    try:
        response = client.post(
            "/v1/endpoints/ep_1/responses",
            headers={"x-api-key": "fc_test"},
            json={"input": "hello"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert called["job_id"] == "job_inline"
    payload = response.json()
    assert payload["id"] == "job_inline"
    assert payload["status"] == "completed"
    assert payload["result_text"] == "done"


def test_submit_response_reuses_idempotent_terminal_job(monkeypatch) -> None:
    def _should_not_create(*_args, **_kwargs):
        raise AssertionError("create_job should not be called")

    def _should_not_process(*_args, **_kwargs):
        raise AssertionError("process_job should not be called")

    monkeypatch.setattr(public_router, "key_allows_endpoint", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(public_router, "enforce_limits", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(public_router, "get_tenant_endpoint", lambda *_args, **_kwargs: SimpleNamespace(id="ep_1"))
    monkeypatch.setattr(public_router, "get_active_version", lambda *_args, **_kwargs: SimpleNamespace(id="ver_1"))
    monkeypatch.setattr(
        public_router,
        "get_idempotent_job_for_key",
        lambda *_args, **_kwargs: SimpleNamespace(**_job_payload("job_existing", status="completed")),
    )
    monkeypatch.setattr(public_router, "create_job", _should_not_create)
    monkeypatch.setattr(public_router, "process_job", _should_not_process)

    app.dependency_overrides[get_api_key_context] = _override_api_context
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    try:
        response = client.post(
            "/v1/endpoints/ep_1/responses",
            headers={"x-api-key": "fc_test", "Idempotency-Key": "idem-abc-1"},
            json={"input": "hello"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["id"] == "job_existing"
    assert response.json()["status"] == "completed"
