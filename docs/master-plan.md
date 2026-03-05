# flash-connector Master Plan (vNext)

> Note (OSS runtime): this document includes historical product planning sections
> (for example wallet / flash-credits concepts) that are not enabled in the current
> open-source implementation. Treat those sections as roadmap notes, not active behavior.

This document is the implementation blueprint for rebuilding **flash-connector** into a world-class, low-effort, multi-tenant “Prompt Endpoint Platform + Training Data Store” with **OpenAI + Azure OpenAI** as the initial providers.

It is intentionally verbose and step-by-step so an engineer (or Codex) can execute it without inventing missing pieces.

Last updated: 2026-02-26

---

## 0) Decisions Locked (From Product Owner)

These are **hard requirements** for this plan:

1. **Admin login is OIDC via Keycloak**.
   - No local email/password login.
   - Admin UI uses OIDC Authorization Code flow (PKCE) against your hosted Keycloak.
2. **`subtenant_code` is allowed on every request**.
   - It is an attribution/billing/analytics dimension.
   - Not a “sub-tenant entity” that users must create first (free-form string).
3. **Provider/model switching is the recommended (safe) behavior**:
   - Switching produces a **new endpoint version** (clone + modify), then **activate**.
   - No hidden routing, no implicit fallback.
4. **Azure auth is API key only** (no Entra ID initially).
5. **Tenant can BYOK** (bring their own provider credentials) **or** use **Flash Credits** (platform-paid) **if and only if** they have prepaid/top-up balance.

---

## 1) What We Are Building (Product Definition)

flash-connector is:

1. **Control plane (Admin Console + Admin API)** for each tenant/workspace to:
   - connect providers (OpenAI, Azure OpenAI)
   - create prompt endpoints (stable IDs) and immutable versions
   - run a “Test Lab” to compare providers/models with identical prompts/inputs
   - activate a version to go live
   - create scoped virtual API keys and set limits
   - review jobs/logs
   - capture training events and export JSONL datasets
   - generate sub-tenant portal links (temporary, scoped) to collect feedback/training edits

2. **Data plane (Public API)** for apps to:
   - submit a job to an endpoint (returns `job_id` immediately)
   - poll by `job_id` (out-of-order completion supported)
   - cancel jobs (best effort)
   - save training events (feedback, tags, edited ideal output, redaction mode)

3. **Training data store** (mandatory feature):
   - tenant/workspace can opt-in to store request/response + feedback
   - strict tenant isolation (no cross-tenant reads/writes)
   - export to JSONL with filters

4. **Billing modes**:
   - BYOK: tenant’s key is used; we track usage + estimated cost.
   - Flash Credits: platform key is used; we debit tenant wallet balance per job.

---

## 2) Experience Principles (Non-Negotiable UX Contract)

1. **No JSON required for the “happy path”.**
   - Advanced JSON is allowed, but must be optional and clearly labeled as advanced.
2. **No hidden system behavior.**
   - Caching, retries, fallbacks, tool access, training capture, redaction: explicit toggles.
3. **One “guided path” to production.**
   - “Connect Provider → Create Endpoint → Test Lab → Promote Winner → Activate → Create Key → Integrate”
4. **Every job explains itself.**
   - selected provider/model, usage, estimated cost, cache-hit (if enabled), provider response id, errors.
5. **Switching is safe by default.**
   - Switching provider/model means “clone version + activate”.
6. **Provider onboarding is bulletproof.**
   - “Test credentials” performs a real call and returns actionable errors.
7. **Adding providers must not require UI rewrites.**
   - Provider config forms come from schemas; adapter plug-in supplies capabilities + docs links.

---

## 3) Key Entities (Mental Model)

Use these names consistently in UI, API, docs, and code:

### Workspace (Tenant)
Represents an organization/team. Owns users, keys, endpoints, jobs, training store, and wallet.

### Provider Connection
Represents a provider integration for a workspace with a billing mode:
- Provider: `openai` or `azure_openai`
- Billing mode: `byok` or `flash_credits`
- Auth mode: `api_key`

Examples a tenant might have simultaneously:
- “OpenAI (BYOK)”
- “OpenAI (Flash Credits)”
- “Azure OpenAI (BYOK)”

### Target (Verified Binding)
Represents a selectable target for inference:
- provider connection
- capability profile (e.g., `responses_chat`, `embeddings`, `tts`)
- target identifier:
  - OpenAI: model id (e.g., `gpt-5-mini`)
  - Azure OpenAI: deployment name (Azure deploys models; the name is tenant-defined)

Targets are created/edited via the UI with a **Verify** action (real upstream call).

### Endpoint
Stable public-facing object.
- Has immutable versions.
- Has one `active_version_id`.

### Endpoint Version
Immutable snapshot of:
- prompt stack (system prompt, persona, context policy)
- runtime params (temperature, max output tokens, timeouts, retries, caching off/on)
- selected target (provider connection + model/deployment)
- training store policy (save default, redaction)

### Job
Execution record. Always:
- correlated by `job_id` (string)
- has status state machine
- stores usage, estimated cost, provider response id
- stores `subtenant_code` (free-form string)

### Training Event
Optional record tied to a job; stores feedback, tags, edited ideal output, full/redacted payloads.

### Sub-tenant Portal Link
Temporary, scoped access for external reviewers/trainers:
- workspace_id + subtenant_code scope
- permissions (view jobs, add feedback, edit ideal output, export)
- expiry

---

## 4) User Journeys (What Must Be Seamless)

### Journey A: Workspace Admin “Go Live in 5 Minutes”
1. Login (Keycloak OIDC)
2. Create workspace
3. Connect provider (OpenAI BYOK or Azure BYOK) and click **Verify**
4. Create endpoint (template-driven)
5. Run Test Lab with a real input
6. Promote best result to Version 1 and activate
7. Create a virtual API key scoped to the endpoint
8. Copy curl snippet and verify end-to-end job completion

### Journey B: Developer Integrates in 10 Lines
1. Receives endpoint id and API key
2. Calls submit job; gets `job_id` immediately
3. Polls until `completed`
4. Saves feedback for training events (optional)
5. Uses `subtenant_code` per request for attribution

### Journey C: “Switch Provider/Model” Without Breaking Clients
1. Open endpoint detail page
2. Click **Switch Target**
3. Pick a new provider connection + model/deployment
4. Run Test Lab side-by-side (old vs new)
5. Click **Promote + Activate** (creates new version and activates)
6. Old version remains in history; diff and audit log captured

### Journey D: External Customer Reviews & Labels Training Data
1. Workspace admin creates portal link for `subtenant_code="ACME-123"` (expires in 7 days)
2. Customer opens link, sees only their jobs and outputs
3. Customer leaves feedback, tags, and ideal output edits
4. Workspace admin exports curated dataset filtered by that subtenant_code

---

## 5) Public API (Data Plane) – Stable Contract

All public API endpoints require `x-api-key`.

### 5.1 Submit Job
`POST /v1/endpoints/{endpoint_id}/jobs`

Body (canonical shape; keep stable):
```json
{
  "input": "string (optional)",
  "messages": [{"role":"user","content":"..."}],
  "metadata": {"any":"json"},
  "subtenant_code": "string (optional)",
  "save_default": true
}
```

Response:
```json
{ "job_id": "job_xxx", "status": "queued" }
```

Rules:
- Exactly one of `input` or `messages` must be provided for chat-style endpoints.
- `subtenant_code` is accepted for every request. Store it on job and training event.
- If the endpoint version is configured for Flash Credits, reject with 402-like error if wallet balance is insufficient (no upstream call).
- Add `Idempotency-Key` header support so clients can safely retry submit without duplicating jobs.

### 5.2 Poll Job
`GET /v1/jobs/{job_id}`

Response always includes:
- `status`
- `result_text` or `error`
- `usage` (normalized)
- `estimated_cost_usd` (always present for completed jobs, estimate is OK)
- `provider_response_id` (if any)
- `cache_hit` + `cache_ref_job_id` if caching enabled and hit occurred
- `subtenant_code`

### 5.3 Cancel Job
`POST /v1/jobs/{job_id}/cancel`

Rules:
- If queued: mark canceled and remove from queue best-effort.
- If running: mark cancel_requested; finalize status after completion best-effort.

### 5.4 Save Training Event
`POST /v1/jobs/{job_id}/save`

Body:
```json
{
  "feedback": {"thumb":"up|down","rating":1},
  "edited_ideal_output": "string (optional)",
  "tags": ["string"],
  "save_mode": "full|redacted"
}
```

Rules:
- Requires either `x-api-key` (scoped to job’s endpoint) or admin OIDC session.
- Records `subtenant_code` from job.

---

## 6) Admin Console UX (Control Plane) – Screen-by-Screen Spec

### 6.1 Navigation (Information Architecture)
Left nav (workspace-scoped):
- Dashboard
- Providers
- Targets
- Endpoints
- Test Lab
- API Keys
- Jobs
- Training
- Sub-tenant Portal
- Usage & Costs
- Settings

### 6.2 Dashboard
Show:
- last 24h jobs (success/fail)
- p50/p95 latency
- usage tokens and estimated cost (split by BYOK vs Flash Credits)
- top endpoints
- top subtenant_codes
- queue health (queued/running)

### 6.3 Providers (Provider Connections)
For each provider connection:
- Display: provider, billing mode, verified status, last verified time
- Actions:
  - Verify now
  - Deactivate
  - Rotate key (BYOK)
- Fields:
  - OpenAI BYOK: API key input (masked), verify
  - OpenAI Flash Credits: no key input, uses platform key; shows “wallet required”
  - Azure OpenAI BYOK:
    - `api_base` (must include scheme)
    - `api_version` (explicit)
    - `api_key`
  - Azure OpenAI Flash Credits:
    - same non-secret fields if needed (base/version)

Important UX requirement:
- Each provider connection card includes a “Docs” link relevant to the provider and capability profiles supported.

### 6.4 Targets
Targets are the “selectable concrete bindings”.

Create target wizard:
1. Choose provider connection
2. Choose capability profile (`responses_chat`, `embeddings`, `tts`, etc.)
3. Enter/select:
   - OpenAI: model id
   - Azure: deployment name
4. Click **Verify Target**
   - perform the smallest real upstream call for that profile
5. Save target

Targets list:
- provider connection name
- profile
- model/deployment
- verified status
- default runtime constraints discovered (if any)

### 6.5 Endpoints
Endpoints list:
- name, description, active version, last used
- one-click “Create” and “Switch Target”

Endpoint creation wizard:
1. Choose template: Chat (Responses), Embeddings, STT, TTS, Realtime Voice
2. Name/description
3. Pick initial target (verified)
4. Prompt builder:
   - system prompt
   - persona (optional)
   - context policy (optional)
5. Runtime controls:
   - temperature
   - max output tokens
   - timeout
   - retries
   - caching toggle (off by default)
   - training save default + redaction
6. Run Test (invokes Test Lab behind the scenes)
7. Create Version 1 + Activate

Endpoint detail:
- Active version summary (provider/model, runtime toggles, training policy)
- Versions list with diff viewer and “Activate” action
- “Switch Target” button:
  - clones active version
  - changes target
  - runs Test Lab
  - activates upon confirmation

### 6.6 Test Lab
Core requirement: compare with identical input across targets/versions.

Test Lab screen:
- choose endpoint + version OR “draft config”
- input editor (messages)
- choose comparison set:
  - active version
  - candidate target(s)
- run matrix
- show side-by-side:
  - output
  - latency
  - usage
  - estimated cost
  - errors
- “Promote winner” creates new version + activate

### 6.7 API Keys
Key creation wizard:
1. Name
2. Scope: all endpoints OR selected endpoints
3. Rate limit per minute
4. Monthly budget (jobs or $) (optional now, required later)
5. Default `subtenant_code` (optional) and “allow override” toggle
6. Training save default for calls made with this key (optional override)
7. Create and show once

Keys list:
- prefix
- active status
- last used
- limits

### 6.8 Jobs
Jobs table filters:
- endpoint
- status
- provider
- billing mode
- subtenant_code
- date range

Job detail:
- request payload (redacted if policy)
- resolved version id + target
- provider response id
- usage + estimated cost + pricing source
- cache-hit explanation (if caching enabled)
- “Save training” shortcut
- “Replay in Test Lab” shortcut (with cache bypass)

### 6.9 Training
Training event filters:
- endpoint/version
- tags
- feedback
- subtenant_code
- date range

Actions:
- export JSONL wizard with preview count + sample lines
- bulk tagging

### 6.10 Sub-tenant Portal
Portal link creation:
- subtenant_code
- expiry (preset: 1 day, 7 days, 30 days)
- permissions:
  - view jobs
  - add feedback/tags
  - edit ideal output
  - export (optional)

Portal user UX:
- only sees jobs/events for the scoped subtenant_code
- simple “review queue” flow for labeling outputs

---

## 7) Auth (Keycloak OIDC) – Concrete Implementation Spec

### 7.1 Admin UI Auth
- Use Keycloak OIDC Authorization Code flow with PKCE.
- Admin UI stores an app session cookie (server-side session or encrypted cookie).
- API validates user session for admin routes.

Config (env vars):
- `OIDC_ISSUER_URL` (Keycloak realm issuer)
- `OIDC_CLIENT_ID`
- `OIDC_CLIENT_SECRET` (only if confidential client)
- `OIDC_REDIRECT_URI`
- `OIDC_POST_LOGOUT_REDIRECT_URI`
- `OIDC_SCOPES` (at least: `openid profile email`)
- `OIDC_METADATA_CACHE_SECONDS`
- `SESSION_SECRET`

User provisioning strategy:
- Option 1 (recommended for OSS): **Just-in-time provisioning** on first login:
  - create local user row with email + role mapping from claims
- Role mapping:
  - Keycloak groups/roles map to `owner/admin/dev/viewer`
  - Store the mapping rules in workspace settings

### 7.2 Programmatic Auth
- Virtual API keys in `x-api-key`.
- Hash at rest, prefix stored for display.
- Keys scoped to endpoints and optionally to default subtenant_code.

### 7.3 Sub-tenant Portal Auth
- Portal links are passwordless magic links:
  - random token, stored hashed
  - server sets a portal session cookie on first visit
  - scope restricted to workspace_id + subtenant_code

---

## 8) Billing + Wallet (BYOK vs Flash Credits)

### 8.1 BYOK
- Tenant provides provider key (OpenAI/Azure).
- Platform validates it (real call), then encrypts and stores.
- No wallet debit.
- UI still shows usage + estimated cost (for transparency).

### 8.2 Flash Credits
- Platform uses system credentials (never stored in DB).
- Require workspace wallet >= estimated minimum before enqueue.
- Debit final cost after completion using usage tokens and pricing table.
- Record ledger transaction with job_id and subtenant_code.

Wallet states:
- `balance` (available)
- `reserved` (held for queued/running jobs)

Debit flow:
1. On submit: reserve funds (conservative estimate or fixed minimum)
2. On completion: compute actual cost, finalize debit, release remainder
3. On failure before upstream call: release reservation

---

## 9) Pricing (Always Present, No Manual Setup)

Hard truth:
- Providers return token usage, not “$ cost per call”.

Policy:
- Always compute `estimated_cost_usd` from token usage + built-in rates.
- Include `pricing_source` field:
  - `builtin_estimate` (default)
  - `tenant_override` (optional)
  - `unknown` (if usage not available for profile)

Pricing data model:
- `provider_slug`
- `capability_profile` (optional)
- `model_pattern` (wildcards)
- `input_per_1m_usd`, `output_per_1m_usd`
- tool/capability surcharges (audio seconds, image generations) per profile
- effective date fields for rate updates

Update mechanism:
- ship rates in repo (versioned)
- allow admin overrides per workspace
- record which rate id was used in the job

---

## 10) Provider Adapter Contract (So We Can Add More Providers Forever)

For each provider plugin:

1. `provider_slug`, `display_name`
2. `docs_links` keyed by capability profile
3. `config_schema` (JSON Schema-like) for non-secret fields
4. `secret_schema` for secrets
5. `verify_connection(connection_settings, secret)` -> success/error with actionable message
6. `verify_target(connection, target)` -> success/error
7. `invoke(profile, target, request)` -> normalized result:
   - `text` (or structured output)
   - `usage` normalized
   - `provider_response_id`
   - `raw` metadata (optional)
8. `normalize_errors` -> stable error codes for UI
9. `pricing_model_id` mapping: model/deployment -> canonical pricing key

Provider onboarding UI must be schema-driven:
- adding a provider should not require a new bespoke UI form

---

## 11) Caching, Retries, Fallbacks (Explicit Only)

Defaults:
- caching: OFF
- fallback routing: OFF
- retries: minimal (1) and explicit

Rules:
- If fallback is off, **never** attempt another model/provider.
- If caching is on, job detail and API response must disclose cache hit.
- “Replay without cache” must exist in Test Lab and job detail.

---

## 12) Data Isolation + Security (Required for OSS Credibility)

1. Tenant scoping at query level in every DB access.
2. Encrypt BYOK provider keys at rest:
   - encryption key lives in server env / secret manager
   - support rotation (store key id metadata)
3. Hash API keys (never store plaintext).
4. Audit logs for:
   - provider connection create/verify/rotate
   - endpoint/version create/activate
   - key create/deactivate
   - portal link create/revoke
5. Least privilege:
   - viewer cannot change configs
   - dev can manage endpoints/keys but not billing settings
6. CSRF protection on admin browser POSTs.
7. CORS: locked down, explicit allow-list.

---

## 13) Build Plan (Step-by-Step Implementation Roadmap)

This roadmap assumes “start from scratch” implementation discipline, even if we refactor an existing codebase.

### Milestone 1: Project skeleton + dev environment
1. Set repo layout: `api/`, `worker/`, `web/`, `sdk/`, `migrations/`, `docs/`
2. Docker compose: postgres, redis, api, worker, web
3. Env management:
   - `.env.example` with all required variables (no magic defaults)
4. CI pipeline scaffolding (lint/test/build)
Acceptance:
- `docker compose up --build` boots all services cleanly.

### Milestone 2: Tenancy + RBAC + OIDC (Keycloak) integration
1. Implement OIDC login callback + session creation
2. User provisioning (JIT) + role mapping from Keycloak claims
3. Workspace selection (if user belongs to multiple workspaces)
4. RBAC guardrails in API and UI
Acceptance:
- Login works end-to-end via Keycloak; RBAC enforced.

### Milestone 3: Provider Connections (BYOK + Flash Credits modes)
1. Provider connection CRUD
2. Secret encryption store for BYOK
3. “Verify connection” endpoint and UI flow
4. Platform credentials wiring for Flash Credits
Acceptance:
- Tenant can verify and save OpenAI and Azure connections.

### Milestone 4: Targets (verified bindings)
1. Target create + verify per capability profile
2. Target list + status UI
Acceptance:
- Tenant can create verified targets for OpenAI models and Azure deployments.

### Milestone 5: Endpoints + Versions + Activation
1. Endpoint CRUD
2. Version create (immutable)
3. Version diff viewer
4. Activation
5. “Switch target” (clone+modify+activate)
Acceptance:
- “Switch target” is one guided flow; no JSON required.

### Milestone 6: Public API + Job runner
1. Virtual API keys (hashing, scoping, rate limits)
2. Submit/poll/cancel endpoints
3. Worker execution using OpenAI + Azure adapters
4. Usage normalization + provider response id storage
Acceptance:
- Curl submit/poll completes and returns stable output and usage.

### Milestone 7: Test Lab + Promote flow
1. Multi-run matrix against multiple targets/versions
2. Side-by-side comparison UI
3. Promote winner -> create new version + activate
Acceptance:
- Switching becomes a “test and promote” workflow.

### Milestone 8: Training store + JSONL export
1. Save training event API
2. Training list + filters
3. Export wizard with preview
4. Subtenant_code as first-class filter
Acceptance:
- Tenant can export a JSONL dataset filtered by endpoint/version/subtenant_code/tags/feedback.

### Milestone 9: Sub-tenant portal
1. Portal link generator + permissions + expiry
2. Portal UI + session auth
3. Portal training workflow
Acceptance:
- External reviewer can label outputs for a subtenant_code with no access to admin settings.

### Milestone 10: Pricing + wallet plumbing
1. Built-in pricing table + job cost estimation always-on
2. Wallet tables and reservation/debit logic for Flash Credits
3. Usage dashboards split by billing mode and subtenant_code
Acceptance:
- Every completed job shows estimated cost; Flash Credits jobs are debited from wallet.

### Milestone 11: OSS polish + docs + hardening
1. Threat model doc + security guidance
2. Docs: provider setup guides and troubleshooting
3. Contribution docs
4. Load testing and performance tuning notes
Acceptance:
- A stranger can self-host and use it successfully from docs.

---

## 14) Provider Scope (Initial) and Expansion Path

Initial providers:
- OpenAI
- Azure OpenAI

Expansion path is strictly controlled by the adapter contract:
- New provider = new adapter + schema + capability matrix + pricing mapping + tests

No provider-specific UI should be hard-coded beyond icons and docs links.

---

## 15) Open Source Strategy (Practical)

Recommended OSS posture:
- Keep core (public API, endpoint/versioning, training store, provider adapters, UI) open source.
- Keep any optional enterprise extensions (SAML, advanced audit exports, org policy packs) in separate repos later if needed.

OSS readiness checklist:
- No secrets in logs
- Example env file is complete
- Demo dataset / seed flow for local dev
- Clear licensing choice (MIT/Apache-2.0) and contributor agreement stance
