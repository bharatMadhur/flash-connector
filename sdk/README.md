# Flash Connector Python SDK

Typed client for flash-connector public APIs.

## What You Get
- Typed models for jobs, training events, and native batches.
- Structured exceptions with API status/detail fields.
- Built-in pollers (`wait_for_job`, `wait_for_batch`).
- Inline response helper (`create_response`).
- Convenience helper (`submit_and_wait`).

## Installation

From repo root:

```bash
pip install -e .
```

Or copy the `sdk/` package into your project.

## Quickstart

```python
from sdk import FlashConnectorClient

with FlashConnectorClient(base_url="http://localhost:8000", api_key="fc_xxx") as client:
    submission = client.submit_job(
        "ep_123",
        input_text="Say hello in one line",
        metadata={"source": "sdk"},
        idempotency_key="req-001",
    )
    job = client.wait_for_job(submission.job_id)
    print(job.status, job.result_text)
```

## API Surface

### `submit_job(...)`
Queue a single async run and return `job_id`.

### `create_response(...)`
Run a single request inline and get full result payload immediately.

### `get_job(job_id)`
Fetch current job state.

### `wait_for_job(job_id, poll_interval_seconds=1.0, timeout_seconds=120.0)`
Poll until terminal state.

### `cancel_job(job_id)`
Request cancellation.

### `save_training(job_id, feedback=None, edited_ideal_output=None, tags=None, save_mode="full", is_few_shot=False)`
Persist feedback/training event for a completed job.

### `submit_batch(...)`
Queue provider-native async batch run.

### `get_batch(batch_id)`, `wait_for_batch(...)`, `cancel_batch(batch_id)`
Batch lifecycle helpers.

### `submit_and_wait(...)`
One-liner for synchronous usage (`create_response` first, then poll fallback).

## Batch Example

```python
from sdk import FlashConnectorClient

with FlashConnectorClient(base_url="http://localhost:8000", api_key="fc_xxx") as client:
    batch = client.submit_batch(
        "ep_123",
        inputs=["Summarize issue A", "Summarize issue B", "Summarize issue C"],
        batch_name="support-bulk-1",
        service_tier="flex",
        metadata={"source": "sdk-batch"},
    )
    detail = client.wait_for_batch(batch.batch_id, timeout_seconds=7200)
    print(detail.status, detail.completed_jobs, detail.failed_jobs)
```

## Training Save Example

```python
from sdk import FlashConnectorClient

with FlashConnectorClient(base_url="http://localhost:8000", api_key="fc_xxx") as client:
    submission = client.submit_job("ep_123", input_text="Draft refund response")
    job = client.wait_for_job(submission.job_id)
    event = client.save_training(
        job.id,
        feedback="thumb_up",
        tags=["refunds", "production"],
        is_few_shot=True,
    )
    print(event.id, event.feedback)
```

## Error Handling

SDK raises typed errors from `sdk/errors.py`:
- API response errors include HTTP status and server detail.
- Wait timeout errors include job/batch id and last known status.

Recommended pattern:

```python
from sdk import FlashConnectorClient
from sdk.errors import FlashConnectorError, JobWaitTimeoutError

try:
    with FlashConnectorClient(base_url="http://localhost:8000", api_key="fc_xxx") as client:
        job = client.submit_and_wait("ep_123", input_text="Hello")
        print(job.result_text)
except JobWaitTimeoutError as exc:
    print("Timed out:", exc.detail)
except FlashConnectorError as exc:
    print("API error:", exc.status_code, exc.detail)
```
