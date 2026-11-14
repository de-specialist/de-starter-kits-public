# Local Provider APIs

The repository root `.env` holds locally available provider settings. Git ignores this file; `.env.example` lists variable names only. These credentials are not currently used by either dbt edition, and dbt does not require them. Do not commit `.env`, paste key values into issues, or expose them in logs. For deployment, store credentials in the deployment platform's secret manager.

## Available providers

| Provider | Variables | Potential use |
|---|---|---|
| NVIDIA NIM | `NVIDIA_API_KEY`, `NVIDIA_BASE_URL` | Model API for natural-language analysis, summarization, and drafting. |
| Z.AI / GLM | `ZAI_API_KEY` | Alternative language-model provider for analysis and generation. |
| Alibaba DashScope | `DASHSCOPE_API_KEY` | Qwen text and multimodal model APIs, subject to account access and quota. |
| OpenRouter | `OPENROUTER_API_KEY` | Unified access and routing across supported models. |
| OpenRouter fallback | `NETA_ASK_FALLBACK_URL`, `NETA_ASK_FALLBACK_MODEL`, `NETA_ASK_FALLBACK_KEY` | Settings copied from the Neta app's optional fallback configuration. |
| MUSE | `MUSE_API_KEY` | Purpose is not identified in this repository. Add only a newly rotated key after confirming the provider and its intended use. |

## Loading credentials locally

Load the file into the current shell only when a future local integration needs it:

```bash
set -a
source .env
set +a
```

Check provider account dashboards for current access, limits, and pricing before sending requests. Do not send private customer records or production credentials to a model without an approved data-handling design.
