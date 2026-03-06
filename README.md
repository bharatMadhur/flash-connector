# flash-connector

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Docker](https://img.shields.io/badge/docker-compose-blue.svg)](docker-compose.yml)

Self-hosted, multi-tenant LLM gateway and prompt endpoint platform with training data capture.

`flash-connector` lets you run your own prompt APIs, connect provider credentials per tenant, issue scoped virtual keys, run async/sync jobs, and export training datasets in JSONL.

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture](#architecture)
- [Quickstart (Local)](#quickstart-local)
- [First End-to-End Run](#first-end-to-end-run)
- [Public API](#public-api)
- [Python SDK](#python-sdk)
- [Configuration](#configuration)
- [Deployment Modes](#deployment-modes)
- [Persistence and Backups](#persistence-and-backups)
- [Multi-Tenancy](#multi-tenancy)
- [Provider and Model Extension (YAML)](#provider-and-model-extension-yaml)
- [Testing](#testing)
- [Security Notes](#security-notes)
- [Contributing](#contributing)
- [License](#license)

## What It Does

- Multi-tenant control plane and API gateway.
- Provider connections per tenant (BYOK and platform-managed modes).
- Versioned prompt APIs (create versions, activate, test, run).
- Virtual API keys (`x-api-key`) with scoped endpoint access.
- Async jobs (`submit -> job_id -> poll`) and sync responses.
- Provider-native batch jobs (submit, poll, cancel).
- Training events + feedback + few-shot flags + JSONL export.
- Provider/model registry via YAML for extensibility.

Current built-in provider profiles:
- OpenAI
- Azure OpenAI (multiple endpoint patterns)
- Azure AI Foundry profile support

## Architecture

```text
Client / SDK / Curl
        |
        v
   FastAPI API + Web UI
        |
   +----+-------------------+
   |                        |
PostgreSQL              Redis + RQ
(metadata, config,      (queue)
 jobs, training)
                            |
                            v
                        Worker
                            |
                            v
                    LLM Provider APIs
```

## Quickstart (Local)

### 1) Prerequisites

- Docker Engine + Docker Compose plugin
- `bash`/`zsh`

### 2) Initialize env and secrets

```bash
cp .env.example .env
./scripts/flashctl init-local
```

`init-local` will:
- generate required secrets if missing,
- enable local login (`test` / `test`),
- set a bootstrap API key for local development.

### 3) Start stack

```bash
./scripts/flashctl up
```

Open:
- Web UI: `http://localhost:8000/login`
- Health: `http://localhost:8000/healthz`
- Ready: `http://localhost:8000/readyz`

## First End-to-End Run

1. Login at `/login`.
2. Go to `Providers` and create a provider connection.
3. Go to `APIs` and create an API endpoint.
4. Create a version and activate it.
5. Go to `API Keys`, create key scoped to that endpoint.
6. Call API from curl/SDK.
7. Save training signal and export JSONL.

## Public API

### Submit async job

```bash
curl -s -X POST http://localhost:8000/v1/endpoints/$ENDPOINT_ID/jobs \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Say hello in one line","metadata":{"source":"smoke"}}'
```

### Poll job

```bash
curl -s http://localhost:8000/v1/jobs/$JOB_ID \
  -H "x-api-key: $API_KEY"
```

### Inline response (single call)

```bash
curl -s -X POST http://localhost:8000/v1/endpoints/$ENDPOINT_ID/responses \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Say hello in one line"}'
```

### Save training event

```bash
curl -s -X POST http://localhost:8000/v1/jobs/$JOB_ID/save \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"feedback":"thumb_up","tags":["gold"],"save_mode":"full","is_few_shot":true}'
```

### Batch APIs

- `POST /v1/endpoints/{endpoint_id}/batches`
- `GET /v1/batches/{batch_id}`
- `POST /v1/batches/{batch_id}/cancel`

## Python SDK

Install from repo root:

```bash
pip install -e .
```

Example:

```python
from sdk import FlashConnectorClient

with FlashConnectorClient(base_url="http://localhost:8000", api_key="fc_xxx") as client:
    submission = client.submit_job(
        "ep_123",
        input_text="Say hello in one line",
        metadata={"source": "sdk"},
    )
    job = client.wait_for_job(submission.job_id)
    print(job.status, job.result_text)
```

See SDK docs in `sdk/README.md`.

## Configuration

Start with `.env.example`.

Required for any non-trivial deployment:
- `SESSION_SECRET`
- `API_KEY_HMAC_SECRET`
- `TENANT_SECRET_ENCRYPTION_KEY`

Important runtime toggles:
- `RUNTIME_MODE=sandbox|production`
- `LOCAL_AUTH_ENABLED=true|false`
- `SINGLE_TENANT_MODE=false` (recommended for OSS usage)

Production expectations:
- `RUNTIME_MODE=production`
- `SESSION_COOKIE_SECURE=true`
- `LOCAL_AUTH_ENABLED=false`
- `LOCAL_BOOTSTRAP_API_KEY` unset
- OIDC configured
- strict `CORS_ORIGINS` (no `*`)

## Deployment Modes

### Standalone (local full stack)

Uses `docker-compose.yml`:
- postgres
- redis
- api
- worker

### Microservice (external DB/Redis)

Uses `docker-compose.microservice.yml`:
- api
- worker

Set external:
- `DATABASE_URL`
- `REDIS_URL`

## Persistence and Backups

Named volumes used by default:
- `postgres_data`
- `tenant_secrets`

Safe lifecycle commands:
- `docker compose down` (keeps volumes)
- `docker compose up -d`

Destructive command:
- `docker compose down -v` (deletes data volumes)

Backup example:

```bash
docker compose exec postgres \
  pg_dump -U ${POSTGRES_USER:-flash} -d ${POSTGRES_DB:-flash_connector} > backup.sql
```

Restore example:

```bash
docker compose exec -T postgres \
  psql -U ${POSTGRES_USER:-flash} -d ${POSTGRES_DB:-flash_connector} < backup.sql
```

## Multi-Tenancy

- Tenant isolation is enforced at query and access layers.
- API keys are tenant-scoped and endpoint-scope aware.
- Tenant hierarchy is supported (parent/child).
- Sub-tenant attribution code can be passed on requests for billing/reporting segmentation.

## Provider and Model Extension (YAML)

Provider catalog is loaded from `providers/`.

### Add model to existing provider

1. Create `providers/<provider_slug>/models/<model-id>.yaml`
2. Define capability + parameter schema
3. Add to `recommended_models` in `providers/<provider_slug>/provider.yaml` (optional)

Example:

```yaml
model: gpt-foo-mini
display_name: GPT Foo Mini
family: gpt-foo
category: reasoning
supports_tools: true
parameters:
  max_output_tokens:
    supported: true
    type: integer
    min: 1
    max: 16384
    default: 512
    description: Output token cap.
  temperature:
    supported: true
    type: number
    min: 0
    max: 2
    default: 0.2
    description: Sampling temperature.
```

### Add new provider profile

1. Create `providers/<provider_slug>/` with:
   - `provider.yaml`
   - `services.yaml`
   - `models/*.yaml`
2. Add runtime profile wiring in:
   - `api/app/core/provider_profiles.py`
   - `api/app/services/providers.py`
   - `api/app/services/provider_validation.py`

## Testing

Run all tests:

```bash
docker compose run --rm --build api python -m pytest -q
```

Suggested checks before merging:
- tests pass
- provider registry tests pass
- at least one real provider connection validation succeeds
- basic API smoke (`submit`, `poll`, `save`) succeeds

## Security Notes

- Virtual API keys are hashed, not stored plaintext.
- Tenant provider secrets are encrypted at rest.
- Session auth uses secure cookie controls.
- CSRF checks are enforced for session-auth unsafe methods.
- Use `RUNTIME_MODE=production` before internet-facing deploys.

## Contributing

If you plan to accept OSS PRs, keep this workflow:

1. Open issue describing problem/change.
2. Keep PRs small and focused.
3. Add/adjust tests for behavior changes.
4. Update README and provider YAML docs for user-facing changes.
5. Keep backward compatibility for public API paths under `/v1/*` where possible.

## License

MIT (`LICENSE`)
