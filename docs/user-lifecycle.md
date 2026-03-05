# flash-connector User Lifecycle

## 1) Platform Owner Bootstrap

1. Start stack with Docker.
2. Login using bootstrap credentials from `.env`.
3. Optionally create child tenants and set inheritance rules.

## 2) Tenant Setup

1. Open **Providers** and configure:
   - `platform` auth (uses env key), or
   - `tenant` auth (tenant-managed encrypted key).
2. Open **APIs** and create API metadata.
3. Create API version with:
   - system prompt
   - provider/model
   - params JSON
   - optional persona/context references
4. Activate version (live configuration).

## 3) Integration Setup

1. Create virtual API key scoped to endpoint(s).
2. Client calls `POST /v1/endpoints/{endpoint_id}/jobs` with `x-api-key`.
3. Client receives `job_id` immediately and polls `GET /v1/jobs/{job_id}`.

## 4) Runtime Operations

1. Jobs run asynchronously in worker.
2. Prompt/version/provider/model are immutable per job record.
3. Fallback routing, caching, and guardrails are applied from version params.

## 5) Training Data Workflow

1. Save training events from completed jobs:
   - feedback
   - edited ideal output
   - tags
   - save mode (`full` or `redacted`)
2. Export JSONL from Training page or `POST /v1/training/export`.

## 6) Nested Tenant Behavior

Each tenant can define:
- `inherit_provider_configs` (true/false)
- `query_params_mode` (`inherit | merge | override`)
- `query_params_json`

Effective query params are resolved through parent lineage and merged into request metadata defaults.

## 7) Test Mode

Use **Test Mode** (`/test-lab`) to run the same effective prompt/input across different provider:model pairs before activation.
