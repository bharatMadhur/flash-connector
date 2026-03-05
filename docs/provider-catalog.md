# flash-connector Provider Catalog

Source of truth:
- Registry loader: `api/app/core/provider_registry.py`
- Catalog wrapper: `api/app/core/provider_catalog.py`
- YAML declarations: `providers/`

This build intentionally supports only:
- `openai`
- `azure_openai`
- `azure_openai_v1`
- `azure_openai_deployment`
- `azure_ai_foundry`

Current runtime execution path in the OSS console is intentionally scoped to:
- `responses_chat` (single-run)
- provider-native async `batch` for responses

Other declared provider services in YAML remain documented for roadmap compatibility and future adapters.

## Routing Policy

- There are no default fallback routes.
- Endpoint execution uses exactly the selected `provider + model`.
- Fallback routing is only used when version `params_json` explicitly sets:
  - `"enable_fallbacks": true`
  - and `fallback_targets` / `fallback_models`.

## Provider YAML Layout

- `providers/openai/provider.yaml`
- `providers/openai/services.yaml`
- `providers/openai/models/*.yaml`
- `providers/azure_openai/provider.yaml`
- `providers/azure_openai/services.yaml`
- `providers/azure_openai/models/*.yaml`
- `providers/azure_openai_v1/provider.yaml`
- `providers/azure_openai_deployment/provider.yaml`
- `providers/azure_ai_foundry/provider.yaml`
- `providers/compatibility/model_equivalence.yaml`

Each model YAML includes:
- capability flags (`supports_realtime`, `supports_vision`, `supports_tools`)
- parameter metadata (`supported`, `type`, `min`, `max`, `default`, `values`)
- behavior notes (`increase_effect`, `decrease_effect`)

This metadata powers UI parameter hints and compatibility suggestions.

## Supported Providers

| Slug | Name | Platform key env | API docs | Realtime docs |
|---|---|---|---|---|
| `openai` | OpenAI | `OPENAI_API_KEY` | https://developers.openai.com/api/reference/overview | https://developers.openai.com/api/docs/guides/realtime |
| `azure_openai` | Azure OpenAI | `AZURE_OPENAI_API_KEY` | https://learn.microsoft.com/en-us/azure/ai-foundry/openai/reference | https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/realtime-audio-websockets |
| `azure_openai_v1` | Azure OpenAI (OpenAI v1) | `AZURE_OPENAI_API_KEY` | https://learn.microsoft.com/en-us/azure/ai-foundry/foundry-models/how-to/use-chat-completions | https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/realtime-audio-websockets |
| `azure_openai_deployment` | Azure OpenAI (Deployment API) | `AZURE_OPENAI_API_KEY` | https://learn.microsoft.com/en-us/azure/ai-foundry/openai/reference | https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/realtime-audio-websockets |
| `azure_ai_foundry` | Azure AI Foundry (Model Inference) | `AZURE_AI_FOUNDRY_API_KEY` | https://learn.microsoft.com/en-us/azure/ai-foundry/model-inference/reference/reference-model-inference-api | https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/realtime-audio-websockets |

## Alias Normalization

The platform normalizes common aliases:
- `azure`, `azureopenai`, `azure-openai` -> `azure_openai`
- `azure-openai-v1` -> `azure_openai_v1`
- `azure-openai-deployment` -> `azure_openai_deployment`
- `azure-foundry`, `foundry` -> `azure_ai_foundry`

Unsupported providers are rejected at validation time.

## Endpoint Defaults

### Azure OpenAI

Preferred:
- `AZURE_OPENAI_BASE_URL=https://<resource>.openai.azure.com/openai/v1`

Legacy compatible:
- `AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com`
- optional `AZURE_OPENAI_API_VERSION` (used only for legacy non-v1 endpoint mode)

Profile behavior:
- `azure_openai` (auto): infers v1/deployment mode from URL shape.
- `azure_openai_v1`: always normalizes to `/openai/v1` and ignores API version.
- `azure_openai_deployment`: always normalizes to resource root and requires API version.
- `azure_ai_foundry`: normalizes Foundry hosts to `/models`.

### Azure AI Foundry

- `AZURE_AI_FOUNDRY_BASE_URL=https://<resource>.services.ai.azure.com/models`
- optional `AZURE_AI_FOUNDRY_API_VERSION`

## Model Catalog

The YAML catalog now includes expanded coverage for both providers:
- GPT-5.x family (`gpt-5.2`, `gpt-5.1`, `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, codex variants)
- GPT-4.1 and GPT-4o families
- o-series (`o3`, `o3-mini`, `o3-pro`, `o4-mini`)
- Realtime (`gpt-realtime`, `gpt-realtime-mini`)
- Audio (`gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `gpt-4o-mini-tts`, plus Azure diarization profile)
- Embeddings (`text-embedding-3-large`, `text-embedding-3-small`, `text-embedding-ada-002`)
- Image generation (`gpt-image-1`)
- Safety/agentic profiles (`omni-moderation-latest`, `computer-use-preview`)

`services.yaml` per provider captures endpoint families and operation paths (Responses, Realtime, Chat Completions legacy, Embeddings, Images, Audio, Files, Fine-tuning, Batch, and provider-specific endpoint modes).

Default in UI: `gpt-5-nano`

Notes:
- OpenAI: use model IDs directly.
- Azure OpenAI: if you use deployment names, enter your exact deployment name in the model field.

## Doc Review Timestamp

Provider YAML and service-model metadata were refreshed against official provider docs on `2026-02-27`.
