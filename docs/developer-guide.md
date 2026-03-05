# flash-connector Developer Guide

This guide is for engineers integrating with flash-connector in production.

## 1) Core Runtime Contract

flash-connector uses an async job contract:

1. Submit to an API endpoint.
2. Get `job_id` immediately with `status=queued`.
3. Poll by `job_id` until terminal (`completed`, `failed`, `canceled`).
4. Optionally save feedback/training data for that job.

This avoids request timeouts and keeps client behavior stable across fast/slow models.

For low-latency/testing workflows, a synchronous inline endpoint is also available:
`POST /v1/endpoints/{endpoint_id}/responses`.

## 2) Auth

### Programmatic auth (public API)
- Header: `x-api-key: fc_xxx`
- Key scope must allow the target API endpoint.

### Admin auth
- Session cookie from web login (`/login`) for admin APIs (`/v1/...` admin surface).

## 3) Public API Endpoints

### Submit Job
`POST /v1/endpoints/{endpoint_id}/jobs`

Body:
```json
{
  "input": "Say hello in one line",
  "metadata": { "source": "integration-test" },
  "subtenant_code": "TEAM-A",
  "save_default": false
}
```

Response:
```json
{
  "job_id": "job_xxx",
  "status": "queued"
}
```

### Immediate Response (Inline)
`POST /v1/endpoints/{endpoint_id}/responses`

Same request body as submit job, but returns full `JobOut` payload directly after inline execution.

### Poll Job
`GET /v1/jobs/{job_id}`

Returns status, result text (if completed), usage, provider response id, and cost estimate.

### Cancel Job
`POST /v1/jobs/{job_id}/cancel`

### Save Training Event
`POST /v1/jobs/{job_id}/save`

Body example:
```json
{
  "feedback": "thumb_up",
  "edited_ideal_output": "Better final answer...",
  "tags": ["support", "gold"],
  "save_mode": "full",
  "is_few_shot": true
}
```

### Native Batch Run
`POST /v1/endpoints/{endpoint_id}/batches`

Supports provider-native asynchronous batches (OpenAI + Azure OpenAI), including `service_tier` (`auto/default/flex/priority`).

Poll/cancel:
- `GET /v1/batches/{batch_id}`
- `POST /v1/batches/{batch_id}/cancel`

## 4) Copy-Paste curl Flows

### Submit + Poll
```bash
curl -s -X POST http://localhost:8000/v1/endpoints/$ENDPOINT_ID/jobs \
  -H "x-api-key: $API_KEY" \
  -H "Idempotency-Key: req-001" \
  -H "Content-Type: application/json" \
  -d '{"input":"Say hello in one line","metadata":{"source":"smoke"}}'

curl -s http://localhost:8000/v1/jobs/$JOB_ID \
  -H "x-api-key: $API_KEY"
```

### Immediate Response
```bash
curl -s -X POST http://localhost:8000/v1/endpoints/$ENDPOINT_ID/responses \
  -H "x-api-key: $API_KEY" \
  -H "Idempotency-Key: req-002-sync" \
  -H "Content-Type: application/json" \
  -d '{"input":"Say hello in one line","metadata":{"source":"sync"}}'
```

### Save Training
```bash
curl -s -X POST http://localhost:8000/v1/jobs/$JOB_ID/save \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"feedback":"thumb_up","tags":["prod"],"save_mode":"full"}'
```

### Provider-Native Batch (Flex)
```bash
curl -s -X POST http://localhost:8000/v1/endpoints/$ENDPOINT_ID/batches \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "batch_name":"bulk-1",
    "service_tier":"flex",
    "items":[
      {"input":"Summarize ticket A"},
      {"input":"Summarize ticket B"}
    ]
  }'
```

## 5) Dry Run + Token Cost Strategy

Use `/playground`:
- **Dry Run Estimator**: no provider completion call; estimates input/output tokens, cost, and cacheability.
- **Live Runner**: same path as clients (`/v1/endpoints/{id}/jobs`) with your real API key.

Optimization guidance:
- Keep request shape stable for better cache hits.
- Avoid volatile metadata fields in cached request paths (`timestamp`, `nonce`, `trace_id`).
- Use batch + `service_tier=flex` for cost-sensitive async workloads.

## 6) Error Handling Model

Common public API status codes:
- `400`: invalid payload / endpoint has no live version.
- `401`: invalid/missing API key.
- `403`: key scope blocks endpoint.
- `404`: endpoint/job/batch not found.
- `429`: rate limit or monthly quota exceeded.
- `500`: internal error.

Recommended client strategy:
- Use idempotency key on submit (`Idempotency-Key`).
- Retry network/5xx failures with bounded backoff.
- Poll until terminal status instead of fixed sleep assumptions.

## 7) SDK (Python)

See `sdk/README.md` for full typed reference.

Quick usage:
```python
from sdk import FlashConnectorClient

with FlashConnectorClient(base_url="http://localhost:8000", api_key="fc_xxx") as client:
    job = client.create_response(
        "endpoint_id",
        input_text="Say hello in one line",
        metadata={"source": "sdk"},
        idempotency_key="req-001-sync",
    )
    print(job.status, job.result_text)
```

## 8) Admin API (Session Auth)

For management automation:
- Provider configs: `/v1/providers`, `/v1/providers/catalog`
- Deployments/targets: `/v1/targets`
- APIs/endpoints + versions + activate: `/v1/endpoints/...`
- API keys: `/v1/api-keys`
- Usage summary: `/v1/usage/summary`
- Training export: `POST /v1/training/export`

Use `/docs` (Swagger) and `/openapi.json` for full schema and response models.
