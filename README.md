# flash-connector

Self-hosted, multi-tenant Prompt Endpoint Platform + Training Data Store.

`flash-connector` lets teams define prompt APIs, issue scoped virtual keys, run async/sync LLM jobs, store training events, and export datasets for fine-tuning.

## What You Get

- Multi-tenant control plane with strict tenant-scoped data access.
- API endpoints with immutable versions and explicit activation.
- Virtual API keys (`x-api-key`) with endpoint scopes, rate limits, and quotas.
- Async jobs (`submit -> job_id -> poll`) and sync responses (`single call`).
- Provider-native batch runs (submit, poll, cancel).
- Training event capture + feedback + JSONL export.
- Few-shot curation from saved training events.
- YAML-driven provider/model catalog so OSS users can add models/providers.

## Supported Runtime Providers (Current)

- OpenAI
- Azure OpenAI (multiple endpoint profiles)
- Azure AI Foundry (registry/runtime profile)

Reference docs in repo:
- `docs/provider-catalog.md`
- `docs/developer-guide.md`
- `docs/user-lifecycle.md`

## Run Modes

### 1) Standalone mode

Runs everything locally with Docker:
- Postgres
- Redis
- API
- Worker

Compose file: `docker-compose.yml`

### 2) Microservice mode

Runs only API + Worker, uses external DB/Redis.

Compose file: `docker-compose.microservice.yml`

Set in `.env`:
- `DATABASE_URL`
- `REDIS_URL`

## 2-Minute Quickstart

```bash
cp .env.example .env
./scripts/flashctl init-local
./scripts/flashctl up
```

Open:
- `http://localhost:8000/login`

Local login (from `.env`):
- `LOCAL_AUTH_USERNAME`
- `LOCAL_AUTH_PASSWORD`

## Operator Helper (`flashctl`)

Use `scripts/flashctl` for reproducible local ops.

Common commands:

```bash
./scripts/flashctl init          # create .env + generate required secrets
./scripts/flashctl init-local    # enable local login + bootstrap key
./scripts/flashctl up            # standalone mode (foreground)
./scripts/flashctl up-bg         # standalone mode (detached)
./scripts/flashctl up-micro      # microservice mode (foreground)
./scripts/flashctl up-micro-bg   # microservice mode (detached)
./scripts/flashctl health        # check /healthz and /readyz
./scripts/flashctl down          # stop standalone stack
./scripts/flashctl down-micro    # stop microservice stack
```

## First-Run Workflow (UI)

1. Login.
2. Go to **Providers** and create at least one ready provider connection.
3. Go to **APIs**, create API, create version, activate it.
4. Go to **API Keys**, create a key scoped to your API.
5. Call public API from curl/SDK.
6. Save training events and export JSONL.

## Public API Quickstart

### Async submit + poll

```bash
# submit
curl -s -X POST http://localhost:8000/v1/endpoints/$ENDPOINT_ID/jobs \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Say hello in one line","metadata":{"source":"smoke"}}'

# poll
curl -s http://localhost:8000/v1/jobs/$JOB_ID \
  -H "x-api-key: $API_KEY"
```

### Sync response (single call)

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
  -d '{"feedback":"thumb_up","tags":["gold"],"save_mode":"full"}'
```

### Export training JSONL (session-auth admin API)

```bash
curl -s -b cookies.txt -X POST http://localhost:8000/v1/training/export \
  -H 'Content-Type: application/json' \
  -d '{"endpoint_id":"'$ENDPOINT_ID'"}' > training.jsonl
```

## Python SDK

Location: `sdk/`

Install from repo root:

```bash
pip install -e .
```

Example:

```python
from sdk import FlashConnectorClient

with FlashConnectorClient(base_url="http://localhost:8000", api_key="fc_xxx") as client:
    # sync
    result = client.create_response("endpoint_id", input_text="Hello")
    print(result.status, result.result_text)

    # async
    submission = client.submit_job("endpoint_id", input_text="Hello")
    job = client.wait_for_job(submission.job_id)
    print(job.status)
```

## Multi-Tenant Behavior

- Tenant isolation is enforced in DB queries and API access checks.
- Hierarchical tenants are supported (parent -> child).
- Query-param inheritance modes per tenant:
  - `inherit`
  - `merge`
  - `override`
- API keys are tenant-scoped and endpoint-scope aware.

Default OSS config is multi-tenant:
- `SINGLE_TENANT_MODE=false`

## Data Persistence and "DB Should Not Get Deleted"

Standalone mode uses named Docker volumes:
- `postgres_data`
- `tenant_secrets`

These survive container restarts and `docker compose down`.

Safe commands:
- `docker compose stop`
- `docker compose down`
- `docker compose up -d`

Destructive command:
- `docker compose down -v` (deletes Postgres + secrets volumes)

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

## Extending the Provider/Model Catalog (OSS)

The platform is registry-driven from `providers/`.

### A) Add a new model under an existing provider

1. Create file: `providers/<provider_slug>/models/<model-id>.yaml`
2. Define capabilities + parameter schema.
3. Add model id to `recommended_models` in `providers/<provider_slug>/provider.yaml` (optional but recommended).

Minimal model YAML example:

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

### B) Add an entirely new provider

1. Create folder: `providers/<new_provider_slug>/`
2. Add:
   - `provider.yaml`
   - `services.yaml`
   - `models/*.yaml`
3. Wire runtime profile and validation paths:
   - `api/app/core/provider_profiles.py`
   - `api/app/services/providers.py`
   - `api/app/services/provider_validation.py`

### Validate catalog changes

```bash
docker compose run --rm --build api \
  pytest -q tests/test_provider_registry_schema.py tests/test_provider_catalog.py tests/test_provider_validation.py
```

## Security Basics

- Virtual keys are hashed (not stored in plaintext).
- Tenant provider keys are encrypted at rest in secrets storage.
- Session auth uses secure cookie settings (`HTTPOnly`, `SameSite=Lax`, configurable `Secure`).
- CSRF protection is enforced for session-authenticated unsafe methods.
- Provider upstream keys are not persisted in normal relational tables.

## Environment Configuration

Start from `.env.example`.

Minimum required:
- `SESSION_SECRET`
- `API_KEY_HMAC_SECRET`
- `TENANT_SECRET_ENCRYPTION_KEY`

Local dev convenience:
- `LOCAL_AUTH_ENABLED=true`
- `LOCAL_AUTH_USERNAME=test`
- `LOCAL_AUTH_PASSWORD=test`

Production hardening:
- `RUNTIME_MODE=production`
- `ENVIRONMENT=production`
- `SESSION_COOKIE_SECURE=true`
- `LOCAL_AUTH_ENABLED=false`
- `LOCAL_BOOTSTRAP_API_KEY=` (empty)
- no wildcard CORS

## Repository Layout

- `api/` FastAPI app + server-rendered UI
- `worker/` RQ worker
- `migrations/` Alembic migrations
- `providers/` provider/model/service YAML registry
- `sdk/` Python SDK
- `tests/` automated tests
- `scripts/flashctl` operator helper
- `docker-compose.yml` standalone stack
- `docker-compose.microservice.yml` API/worker only

## Deployment (DigitalOcean / Any VM)

1. Provision VM.
2. Install Docker + Docker Compose plugin.
3. Clone repo.
4. Configure `.env` with production secrets and DB/Redis URLs.
5. Run:

```bash
docker compose up -d --build
```

6. Put reverse proxy (Nginx/Caddy) in front of `:8000` with TLS.
7. Schedule Postgres backups.

## Testing

```bash
docker compose run --rm --build api python -m pytest -q
```

## Git/Repo Strategy (Landing vs Product)

Recommended split:
- `flash-connector` -> runtime/API/worker/SDK (this repo)
- `flash-connector-site` -> landing/marketing/docs website

Keep release cadence independent. Landing repo should link to tagged product releases.

## License

MIT (`LICENSE`)
