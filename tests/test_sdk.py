import json
import os
import sys
from typing import Any

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sdk import AuthenticationError, FlashConnectorClient
from sdk.models import BatchDetail, JobDetail


def _job_payload(status: str) -> dict[str, Any]:
    return {
        "id": "job_123",
        "tenant_id": "tenant_1",
        "endpoint_id": "ep_1",
        "endpoint_version_id": "ver_1",
        "billing_mode": "byok",
        "reserved_cost_usd": 0.0,
        "request_api_key_id": "key_1",
        "idempotency_key": "idem_1",
        "status": status,
        "request_json": {"input": "hello"},
        "request_hash": None,
        "cache_hit": False,
        "cached_from_job_id": None,
        "result_text": "done" if status == "completed" else None,
        "error": None,
        "usage_json": {"prompt_tokens": 10, "completion_tokens": 4},
        "estimated_cost_usd": 0.00001234,
        "provider_response_id": "resp_1",
        "provider_used": "openai",
        "model_used": "gpt-4.1-mini",
        "created_at": "2026-02-22T12:00:00+00:00",
        "started_at": "2026-02-22T12:00:01+00:00",
        "finished_at": "2026-02-22T12:00:03+00:00" if status == "completed" else None,
    }


def _batch_payload(status: str) -> dict[str, Any]:
    return {
        "id": "pbr_123",
        "tenant_id": "tenant_1",
        "endpoint_id": "ep_1",
        "endpoint_version_id": "ver_1",
        "provider_slug": "openai",
        "provider_config_id": "cfg_1",
        "model_used": "gpt-5-mini",
        "status": status,
        "completion_window": "24h",
        "provider_batch_id": "batch_abc",
        "input_file_id": "file_in_1",
        "output_file_id": "file_out_1" if status == "completed" else None,
        "error_file_id": None,
        "request_json": {"batch_name": "smoke", "metadata": {"service_tier": "flex"}},
        "result_json": {"id": "batch_abc", "status": status},
        "error": None,
        "total_jobs": 3,
        "completed_jobs": 3 if status == "completed" else 0,
        "failed_jobs": 0,
        "canceled_jobs": 0,
        "cancel_requested": False,
        "created_by_user_id": None,
        "created_at": "2026-02-27T12:00:00+00:00",
        "started_at": "2026-02-27T12:00:01+00:00",
        "finished_at": "2026-02-27T12:00:15+00:00" if status == "completed" else None,
        "last_polled_at": "2026-02-27T12:00:15+00:00",
        "next_poll_at": None if status == "completed" else "2026-02-27T12:00:20+00:00",
        "updated_at": "2026-02-27T12:00:15+00:00",
    }


def test_submit_job_and_get_job() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/endpoints/ep_1/jobs":
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"job_id": "job_123", "status": "queued"})
        if request.method == "GET" and request.url.path == "/v1/jobs/job_123":
            return httpx.Response(200, json=_job_payload("completed"))
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    sdk = FlashConnectorClient(
        base_url="http://localhost:8000",
        api_key="fc_test_123",
        transport=httpx.MockTransport(handler),
    )

    submission = sdk.submit_job("ep_1", input_text="hello", metadata={"trace_id": "t1"})
    assert submission.job_id == "job_123"
    assert submission.status == "queued"
    assert captured["headers"]["x-api-key"] == "fc_test_123"
    assert captured["body"]["input"] == "hello"
    assert captured["body"]["metadata"] == {"trace_id": "t1"}

    job = sdk.get_job("job_123")
    assert isinstance(job, JobDetail)
    assert job.is_terminal is True
    assert job.is_success is True
    assert job.result_text == "done"
    assert job.estimated_cost_usd == pytest.approx(0.00001234)
    assert job.idempotency_key == "idem_1"


def test_create_response_inline() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/endpoints/ep_1/responses":
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=_job_payload("completed"))
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    sdk = FlashConnectorClient(
        base_url="http://localhost:8000",
        api_key="fc_test_123",
        transport=httpx.MockTransport(handler),
    )

    job = sdk.create_response("ep_1", input_text="hello", metadata={"trace_id": "inline-1"})
    assert job.status == "completed"
    assert job.result_text == "done"
    assert captured["headers"]["x-api-key"] == "fc_test_123"
    assert captured["body"]["input"] == "hello"
    assert captured["body"]["metadata"] == {"trace_id": "inline-1"}


def test_wait_for_job_polling() -> None:
    statuses = iter(["queued", "running", "completed"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=_job_payload(next(statuses)))

    sdk = FlashConnectorClient(
        base_url="http://localhost:8000",
        api_key="fc_test_123",
        transport=httpx.MockTransport(handler),
    )

    job = sdk.wait_for_job("job_123", poll_interval_seconds=0.001, timeout_seconds=1)
    assert job.status == "completed"
    assert job.is_success is True


def test_cancel_and_save_training() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"job_id": "job_123", "status": "canceled"})
        if request.method == "POST" and request.url.path.endswith("/save"):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["save_mode"] == "redacted"
            return httpx.Response(
                200,
                json={
                    "id": "tr_1",
                    "tenant_id": "tenant_1",
                    "endpoint_id": "ep_1",
                    "endpoint_version_id": "ver_1",
                    "job_id": "job_123",
                    "input_json": {"input": "hello"},
                    "output_text": "done",
                    "feedback": "thumbs_up",
                    "edited_ideal_output": None,
                    "tags": ["gold"],
                    "created_at": "2026-02-22T12:05:00+00:00",
                    "save_mode": "redacted",
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    sdk = FlashConnectorClient(
        base_url="http://localhost:8000",
        api_key="fc_test_123",
        transport=httpx.MockTransport(handler),
    )

    canceled = sdk.cancel_job("job_123")
    assert canceled.status == "canceled"

    event = sdk.save_training("job_123", feedback="thumbs_up", tags=["gold"], save_mode="redacted")
    assert event.id == "tr_1"
    assert event.tags == ["gold"]


def test_submit_requires_input_or_messages() -> None:
    sdk = FlashConnectorClient(base_url="http://localhost:8000", api_key="fc_test_123")
    with pytest.raises(ValueError):
        sdk.submit_job("ep_1")
    sdk.close()


def test_submit_job_passes_idempotency_key_header() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"job_id": "job_123", "status": "queued"})

    sdk = FlashConnectorClient(
        base_url="http://localhost:8000",
        api_key="fc_test_123",
        transport=httpx.MockTransport(handler),
    )

    submission = sdk.submit_job("ep_1", input_text="hello", idempotency_key="idem-abc-123")
    assert submission.job_id == "job_123"
    assert captured["headers"]["idempotency-key"] == "idem-abc-123"


def test_submit_and_wait_prefers_sync_endpoint() -> None:
    called: dict[str, int] = {"responses": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/endpoints/ep_1/responses":
            called["responses"] += 1
            return httpx.Response(200, json=_job_payload("completed"))
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    sdk = FlashConnectorClient(
        base_url="http://localhost:8000",
        api_key="fc_test_123",
        transport=httpx.MockTransport(handler),
    )

    job = sdk.submit_and_wait("ep_1", input_text="hello")
    assert called["responses"] == 1
    assert job.status == "completed"
    assert job.result_text == "done"


def test_error_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid API key"})

    sdk = FlashConnectorClient(
        base_url="http://localhost:8000",
        api_key="bad_key",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AuthenticationError):
        sdk.get_job("job_1")


def test_submit_and_get_batch() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/endpoints/ep_1/batches":
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "batch_id": "pbr_123",
                    "status": "queued",
                    "provider_slug": "openai",
                    "model_used": "gpt-5-mini",
                    "total_jobs": 3,
                },
            )
        if request.method == "GET" and request.url.path == "/v1/batches/pbr_123":
            return httpx.Response(200, json=_batch_payload("completed"))
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    sdk = FlashConnectorClient(
        base_url="http://localhost:8000",
        api_key="fc_test_123",
        transport=httpx.MockTransport(handler),
    )
    submission = sdk.submit_batch("ep_1", inputs=["a", "b", "c"], service_tier="flex")
    assert submission.batch_id == "pbr_123"
    assert submission.total_jobs == 3
    assert captured["body"]["metadata"]["service_tier"] == "flex"
    assert len(captured["body"]["items"]) == 3

    batch = sdk.get_batch("pbr_123")
    assert isinstance(batch, BatchDetail)
    assert batch.is_terminal is True
    assert batch.is_success is True
    assert batch.completed_jobs == 3


def test_cancel_batch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/batches/pbr_123/cancel":
            return httpx.Response(200, json={"batch_id": "pbr_123", "status": "canceling"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    sdk = FlashConnectorClient(
        base_url="http://localhost:8000",
        api_key="fc_test_123",
        transport=httpx.MockTransport(handler),
    )
    canceled = sdk.cancel_batch("pbr_123")
    assert canceled.batch_id == "pbr_123"
    assert canceled.status == "canceling"


def test_wait_for_batch_polling() -> None:
    statuses = iter(["queued", "processing", "completed"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=_batch_payload(next(statuses)))

    sdk = FlashConnectorClient(
        base_url="http://localhost:8000",
        api_key="fc_test_123",
        transport=httpx.MockTransport(handler),
    )

    batch = sdk.wait_for_batch("pbr_123", poll_interval_seconds=0.001, timeout_seconds=1)
    assert batch.status == "completed"
