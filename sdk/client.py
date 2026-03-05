"""Synchronous Python SDK for flash-connector public APIs.

This client intentionally stays simple:
- one reusable ``httpx.Client`` per SDK instance
- typed dataclass return objects from ``sdk.models``
- explicit wait helpers for async job and batch workflows
- conservative retry behavior for transient network/server failures

Retry policy is designed to avoid duplicate non-idempotent writes:
- GET/HEAD/OPTIONS are retried on transient failures
- POST/PUT/PATCH/DELETE are retried only when an ``Idempotency-Key`` is present
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .errors import BatchWaitTimeoutError, FlashConnectorError, JobWaitTimeoutError, raise_for_response_error
from .models import (
    BatchCancellation,
    BatchDetail,
    BatchSubmission,
    JobCancellation,
    JobDetail,
    JobSubmission,
    SaveMode,
    TrainingEvent,
)


class FlashConnectorClient:
    """High-level API client for creating runs, batches, and training events.

    Args:
        base_url: flash-connector API base URL, e.g. ``http://localhost:8000``.
        api_key: Virtual API key used in ``x-api-key`` header.
        timeout: Per-request timeout passed to ``httpx``.
        user_agent: User-Agent header value sent on each request.
        max_request_retries: Maximum retry attempts for transient failures.
        retry_backoff_seconds: Base delay between retries (exponential backoff).
        retry_on_status_codes: HTTP status codes considered retryable.
        transport: Optional custom transport (primarily for tests).
        client: Optional pre-built ``httpx.Client``. If set, SDK does not own lifecycle.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        user_agent: str = "flash-connector-sdk/0.2",
        max_request_retries: int = 2,
        retry_backoff_seconds: float = 0.4,
        retry_on_status_codes: tuple[int, ...] = (408, 409, 429, 500, 502, 503, 504),
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """Initialize SDK client state and HTTP transport."""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.user_agent = user_agent
        self.max_request_retries = max(0, int(max_request_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.retry_on_status_codes = tuple({int(code) for code in retry_on_status_codes})

        if client is not None and transport is not None:
            raise ValueError("Provide either `client` or `transport`, not both")

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.Client(timeout=timeout, transport=transport)
            self._owns_client = True

    def close(self) -> None:
        """Close internal HTTP client if this SDK instance created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "FlashConnectorClient":
        """Support ``with FlashConnectorClient(...) as client`` usage."""
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Context manager exit hook that closes owned HTTP resources."""
        self.close()

    def submit_job(
        self,
        endpoint_id: str,
        *,
        input_text: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        subtenant_code: str | None = None,
        idempotency_key: str | None = None,
        save_default: bool = False,
    ) -> JobSubmission:
        """Submit an async job and return immediate queue acknowledgement."""
        if input_text is None and not messages:
            raise ValueError("Either input_text or messages must be provided")

        payload: dict[str, Any] = {
            "metadata": metadata or {},
            "subtenant_code": subtenant_code,
            "save_default": save_default,
        }
        if input_text is not None:
            payload["input"] = input_text
        if messages is not None:
            payload["messages"] = messages

        extra_headers: dict[str, str] | None = None
        if idempotency_key is not None and idempotency_key.strip():
            extra_headers = {"Idempotency-Key": idempotency_key.strip()}

        data = self._request(
            "POST",
            f"/v1/endpoints/{endpoint_id}/jobs",
            json=payload,
            headers=extra_headers,
        )
        return JobSubmission.from_api(data)

    def create_response(
        self,
        endpoint_id: str,
        *,
        input_text: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        subtenant_code: str | None = None,
        idempotency_key: str | None = None,
        save_default: bool = False,
    ) -> JobDetail:
        """Run one request inline through sync endpoint and return final job record."""
        if input_text is None and not messages:
            raise ValueError("Either input_text or messages must be provided")

        payload: dict[str, Any] = {
            "metadata": metadata or {},
            "subtenant_code": subtenant_code,
            "save_default": save_default,
        }
        if input_text is not None:
            payload["input"] = input_text
        if messages is not None:
            payload["messages"] = messages

        extra_headers: dict[str, str] | None = None
        if idempotency_key is not None and idempotency_key.strip():
            extra_headers = {"Idempotency-Key": idempotency_key.strip()}

        data = self._request(
            "POST",
            f"/v1/endpoints/{endpoint_id}/responses",
            json=payload,
            headers=extra_headers,
        )
        return JobDetail.from_api(data)

    def submit_batch(
        self,
        endpoint_id: str,
        *,
        inputs: list[str] | None = None,
        items: list[dict[str, Any]] | None = None,
        batch_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        subtenant_code: str | None = None,
        save_default: bool = False,
        service_tier: str = "auto",
    ) -> BatchSubmission:
        """Submit provider-native batch request for deferred low-cost processing."""
        normalized_items: list[dict[str, Any]] = []
        if inputs:
            for input_text in inputs:
                normalized_items.append({"input": input_text})
        if items:
            normalized_items.extend(items)
        if not normalized_items:
            raise ValueError("At least one batch item is required")

        normalized_tier = (service_tier or "auto").strip().lower()
        if normalized_tier not in {"auto", "default", "flex", "priority"}:
            normalized_tier = "auto"

        shared_metadata = dict(metadata or {})
        shared_metadata["service_tier"] = normalized_tier

        payload: dict[str, Any] = {
            "items": normalized_items,
            "batch_name": batch_name,
            "metadata": shared_metadata,
            "subtenant_code": subtenant_code,
            "save_default": save_default,
            "service_tier": normalized_tier,
            "completion_window": "24h",
        }
        data = self._request("POST", f"/v1/endpoints/{endpoint_id}/batches", json=payload)
        return BatchSubmission.from_api(data)

    def get_job(self, job_id: str) -> JobDetail:
        """Fetch a job by id."""
        data = self._request("GET", f"/v1/jobs/{job_id}")
        return JobDetail.from_api(data)

    def get_batch(self, batch_id: str) -> BatchDetail:
        """Fetch a provider batch run by id."""
        data = self._request("GET", f"/v1/batches/{batch_id}")
        return BatchDetail.from_api(data)

    def cancel_job(self, job_id: str) -> JobCancellation:
        """Request cancellation for one async job."""
        data = self._request("POST", f"/v1/jobs/{job_id}/cancel")
        return JobCancellation.from_api(data)

    def cancel_batch(self, batch_id: str) -> BatchCancellation:
        """Request cancellation for one provider-native batch run."""
        data = self._request("POST", f"/v1/batches/{batch_id}/cancel")
        return BatchCancellation.from_api(data)

    def wait_for_job(
        self,
        job_id: str,
        *,
        poll_interval_seconds: float = 1.0,
        timeout_seconds: float | None = 120.0,
    ) -> JobDetail:
        """Poll a job until terminal status or timeout."""
        started = time.monotonic()

        while True:
            job = self.get_job(job_id)
            if job.is_terminal:
                return job

            if timeout_seconds is not None and (time.monotonic() - started) >= timeout_seconds:
                raise JobWaitTimeoutError(
                    f"Timed out waiting for job {job_id}",
                    detail={"job_id": job_id, "last_status": job.status},
                )
            time.sleep(poll_interval_seconds)

    def wait_for_batch(
        self,
        batch_id: str,
        *,
        poll_interval_seconds: float = 3.0,
        timeout_seconds: float | None = 3600.0,
    ) -> BatchDetail:
        """Poll a provider batch until terminal status or timeout."""
        started = time.monotonic()

        while True:
            batch = self.get_batch(batch_id)
            if batch.is_terminal:
                return batch

            if timeout_seconds is not None and (time.monotonic() - started) >= timeout_seconds:
                raise BatchWaitTimeoutError(
                    f"Timed out waiting for batch {batch_id}",
                    detail={"batch_id": batch_id, "last_status": batch.status},
                )
            time.sleep(poll_interval_seconds)

    def submit_and_wait(
        self,
        endpoint_id: str,
        *,
        input_text: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        subtenant_code: str | None = None,
        idempotency_key: str | None = None,
        save_default: bool = False,
        poll_interval_seconds: float = 1.0,
        timeout_seconds: float | None = 120.0,
        prefer_sync_endpoint: bool = True,
    ) -> JobDetail:
        """Submit a request and wait for final result with sync-first behavior.

        When ``prefer_sync_endpoint`` is true, this uses the synchronous
        ``/responses`` endpoint first, then falls back to polling when needed.
        """
        if prefer_sync_endpoint:
            inline_job = self.create_response(
                endpoint_id,
                input_text=input_text,
                messages=messages,
                metadata=metadata,
                subtenant_code=subtenant_code,
                idempotency_key=idempotency_key,
                save_default=save_default,
            )
            if inline_job.is_terminal:
                return inline_job
            return self.wait_for_job(
                inline_job.id,
                poll_interval_seconds=poll_interval_seconds,
                timeout_seconds=timeout_seconds,
            )

        submission = self.submit_job(
            endpoint_id,
            input_text=input_text,
            messages=messages,
            metadata=metadata,
            subtenant_code=subtenant_code,
            idempotency_key=idempotency_key,
            save_default=save_default,
        )
        return self.wait_for_job(
            submission.job_id,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
        )

    def save_training(
        self,
        job_id: str,
        *,
        feedback: str | None = None,
        edited_ideal_output: str | None = None,
        tags: list[str] | None = None,
        save_mode: SaveMode = "full",
        is_few_shot: bool = False,
    ) -> TrainingEvent:
        """Persist one job into training store with optional feedback metadata."""
        payload = {
            "feedback": feedback,
            "edited_ideal_output": edited_ideal_output,
            "tags": tags or [],
            "save_mode": save_mode,
            "is_few_shot": is_few_shot,
        }
        data = self._request("POST", f"/v1/jobs/{job_id}/save", json=payload)
        return TrainingEvent.from_api(data)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Build common request headers for all SDK calls."""
        return {
            "x-api-key": self.api_key,
            "user-agent": self.user_agent,
            **(extra or {}),
        }

    def _url(self, path: str) -> str:
        """Normalize absolute/relative paths into final request URL."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}{path}"

    def _can_retry_request(self, method: str, headers: dict[str, str] | None) -> bool:
        """Return True when retrying request is safe and duplication-resistant."""
        normalized = method.upper()
        if normalized in {"GET", "HEAD", "OPTIONS"}:
            return True
        if normalized not in {"POST", "PUT", "PATCH", "DELETE"}:
            return False
        if not headers:
            return False

        for key, value in headers.items():
            if key.lower() == "idempotency-key" and str(value).strip():
                return True
        return False

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Execute one HTTP request with transient retry handling.

        Retries are only applied when ``_can_retry_request`` returns true.
        Final API responses are mapped through ``raise_for_response_error`` so
        callers always receive typed SDK exceptions.
        """
        extra_headers = kwargs.pop("headers", None)
        can_retry = self._can_retry_request(method, extra_headers)
        max_attempts = 1 + self.max_request_retries if can_retry else 1

        last_transport_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = self._client.request(
                    method,
                    self._url(path),
                    headers=self._headers(extra_headers),
                    **kwargs,
                )
            except httpx.RequestError as exc:
                last_transport_error = exc
                if attempt >= max_attempts:
                    raise FlashConnectorError(
                        f"Request failed after {attempt} attempt(s): {exc}",
                        detail={"path": path, "method": method.upper()},
                    ) from exc
                sleep_seconds = self.retry_backoff_seconds * (2 ** (attempt - 1))
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                continue

            if response.status_code in self.retry_on_status_codes and attempt < max_attempts:
                sleep_seconds = self.retry_backoff_seconds * (2 ** (attempt - 1))
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                continue

            raise_for_response_error(response)
            if response.status_code == 204:
                return {}
            return dict(response.json())

        if last_transport_error is not None:
            raise FlashConnectorError(
                f"Request failed: {last_transport_error}",
                detail={"path": path, "method": method.upper()},
            ) from last_transport_error
        raise FlashConnectorError("Request failed without response")
