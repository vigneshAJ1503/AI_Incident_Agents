# LLM providers: pick the platform your company approved

The agents need one hosted chat model with **tool calling**. Which platform serves it is
profile configuration only (`llm:` in `profiles/<company>/profile.yaml`): agents, prompts,
the orchestrator and evals never change (ADR-0017). Tests and replays use a fake LLM and
spend zero tokens.

> **Project rule:** no model runs on the developer's laptop. A company's own inference
> server (vLLM, LiteLLM, Ollama on a GPU host) is fine: it is just another hosted endpoint.

## Which one?

| Situation | `llm.provider` | Notes |
|-----------|----------------|-------|
| Personal use, demos, this repo's defaults | `openai_compat` + **Groq** or **Gemini** free tier | no credit card; rate limits apply (docs/setup/zero-cost.md) |
| Company standardised on Claude, direct contract with Anthropic | `anthropic` | workspace API key with a spend limit; optional company gateway via `base_url` |
| Company runs on AWS / data must stay in the AWS account | `bedrock` | no API key: IAM role/SSO via the AWS credential chain; VPC endpoint via `base_url` |
| Company runs on Azure / Microsoft enterprise agreement | `azure_openai` | resource endpoint + deployment names + `api-version` |
| OpenAI, OpenRouter, a company LLM gateway, a self-hosted vLLM/LiteLLM/Ollama server | `openai_compat` | any OpenAI-compatible Chat Completions API with tool calling |

The model must support tool calling (function calling). Three roles, each a model id (or,
on Azure, a deployment name): `fast` (planner fallback, cheap extraction), `agent` (the
seven specialists), `rca` (ranking root causes). `fast` and `rca` fall back to `agent`.

## Configure

Copy the matching `llm` block from `profiles/_template/profile.yaml` into your profile,
set the variables (names in `profiles/_template/.env.example`) in `profiles/<company>/.env`
or your secret manager, then:

```bash
uv run aiops profile validate --profile acme      # which field / env var is missing
uv run aiops llm ping --profile acme --role fast  # one tiny request: latency + tokens
uv run aiops doctor --profile acme                # provider, endpoint, model per role, ping
curl -s localhost:8000/api/health | jq .llm       # {"provider": "...", "configured": true}
```

### OpenAI-compatible (`openai_compat`)

```yaml
llm:
  provider: openai_compat
  base_url: ${OPENAI_COMPAT_BASE_URL}
  api_key: ${OPENAI_COMPAT_API_KEY}
  models: {fast: ${LLM_MODEL_FAST:-}, agent: ${LLM_MODEL_AGENT}, rca: ${LLM_MODEL_RCA:-}}
```

| Service | `OPENAI_COMPAT_BASE_URL` |
|---------|--------------------------|
| Groq (free tier) | `https://api.groq.com/openai/v1` |
| Google Gemini (free tier) | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| OpenAI | `https://api.openai.com/v1` |
| company vLLM / LiteLLM / Ollama **server** | its URL + `/v1`, e.g. `https://llm.internal.acme.com/v1` |

Pick model ids with tool calling from the service's model list. For Ollama/vLLM servers,
the served model must have a tool-calling chat template (e.g. vLLM `--enable-auto-tool-choice`).

### Anthropic

```yaml
llm:
  provider: anthropic
  api_key: ${ANTHROPIC_API_KEY}          # SECRET
  base_url: ${ANTHROPIC_BASE_URL:-}      # optional: approved gateway/proxy
  models: {fast: ${LLM_MODEL_FAST:-}, agent: ${LLM_MODEL_AGENT}, rca: ${LLM_MODEL_RCA:-}}
```

Example model ids (check the Anthropic models overview for current ones):
`LLM_MODEL_FAST=claude-haiku-4-5`, `LLM_MODEL_AGENT=claude-sonnet-5`,
`LLM_MODEL_RCA=claude-sonnet-5`. The official SDK retries 429, 5xx and 529 "overloaded"
with backoff (`max_retries`). Models that reject forced tool use (e.g. Claude Opus 5.5)
need `forced_tool_choice: false`. No `temperature` is sent unless the profile sets one.

### Amazon Bedrock

```yaml
llm:
  provider: bedrock
  base_url: ${BEDROCK_ENDPOINT_URL:-}    # optional: VPC/PrivateLink endpoint
  models: {fast: ${LLM_MODEL_FAST:-}, agent: ${LLM_MODEL_AGENT}, rca: ${LLM_MODEL_RCA:-}}
```

- **No key in the profile** (`llm.api_key` is rejected): credentials and region come from
  the standard AWS chain: `AWS_PROFILE` + `aws sso login` on a workstation, IRSA / Pod
  Identity on EKS, the task/instance role on ECS/EC2. Region: `AWS_REGION` (or
  `AWS_DEFAULT_REGION`, or the profile's region).
- Uses the **Converse API**, so any Bedrock model with tool use works (Claude, Amazon
  Nova, Llama, Mistral). `models` are model ids or **inference-profile** ids/ARNs; copy
  them from the Bedrock console (Model catalog / Cross-region inference), e.g.
  `us.anthropic.claude-...` for a US cross-region profile.
- Enable model access for the account/region first; `AccessDeniedException` says so.
- Least privilege: an IAM policy with `bedrock:InvokeModel` on those model/inference-profile
  ARNs only.
- Models without `toolChoice: any` support need `forced_tool_choice: false`.

### Azure OpenAI

```yaml
llm:
  provider: azure_openai
  base_url: ${AZURE_OPENAI_ENDPOINT}         # https://<resource>.openai.azure.com
  api_key: ${AZURE_OPENAI_API_KEY}           # SECRET
  api_version: ${AZURE_OPENAI_API_VERSION}   # e.g. 2024-10-21 (see Azure's API lifecycle)
  models: {fast: ${LLM_MODEL_FAST:-}, agent: ${LLM_MODEL_AGENT}, rca: ${LLM_MODEL_RCA:-}}
```

`models` are **deployment names** from Azure AI Foundry / the Azure portal, not model
names. Entra ID (keyless) auth is not supported yet: use a key from Key Vault.

## A second provider as a fallback (`llm.fallbacks`)

Free tiers cap tokens per minute **and per day**, per provider. Extra keys for the same
provider (`fallback_api_keys`) help with the per-minute cap. When a whole provider's quota is
spent, `llm.fallbacks` moves each call to the next provider instead:

```yaml
llm:
  provider: openai_compat          # Groq first
  base_url: https://api.groq.com/openai/v1
  api_key: ${OPENAI_COMPAT_API_KEY:-}
  models: {agent: openai/gpt-oss-20b}
  fallbacks:                       # then Gemini's free tier
    - provider: openai_compat
      base_url: https://generativelanguage.googleapis.com/v1beta/openai/
      api_key: ${GEMINI_API_KEY:-}
      models: {agent: gemini-2.5-flash}
```

- Each entry is a full provider config, with its own models, keys, limits and `extra`. Any
  provider type works: an enterprise profile can fall back from Azure OpenAI to Bedrock.
- A rate-limited provider rests for 30 s before it is tried again, and outages (5xx,
  timeouts) move on without resting. A rejected tool call is **not** a reason to switch: the
  agent corrects the same model.
- An entry whose key isn't set is skipped, so `profiles/local` lists Gemini and it stays
  inert until you set `GEMINI_API_KEY` (free: <https://aistudio.google.com/apikey>).
- The model that actually answered shows in the report's *Cost & tokens* card, the
  `aiops_llm_*` metrics and the traces. Only when every provider fails does an agent finish
  with the deterministic analysis.

## Data residency and privacy

- Tool output is **redacted before it reaches any LLM** (`guardrails.redact`: emails, IPs,
  JWTs, bearer tokens, API keys, card numbers) and capped (`max_tool_output_chars`).
- Bedrock and Azure OpenAI process data in the region you choose (Bedrock cross-region
  inference profiles may route within a geography: `us.`, `eu.`, `apac.`). Keep traffic
  private with a VPC endpoint (`base_url`) or Azure private networking.
- Anthropic: check your org's data-retention terms and region options; a company gateway
  (`base_url`) can add logging/DLP.
- Free tiers (Groq, Gemini) may use prompts to improve services: personal/demo data only.

## Cost control knobs

| Knob | Where | Effect |
|------|-------|--------|
| model per role | `llm.models.{fast,agent,rca}` | a small model for `fast`, the strongest only for `rca` |
| per-investigation budget | `orchestrator.max_tokens` | RCA falls back to deterministic ranking beyond it |
| per-agent budget | `agents.<name>.limits.{max_steps,max_tool_calls,max_tokens}` | caps each specialist's loop |
| skip the LLM where rules suffice | `orchestrator.llm_planner_fallback`, `orchestrator.llm_rca` | `false` = deterministic only |
| fewer agents | `enabled: false` on unused capabilities | no LLM calls for them |
| retries | `llm.max_retries`, `llm.timeout_s` | bound the worst-case wall clock and spend |
| provider-side | Anthropic workspace spend limits, AWS Budgets, Azure cost alerts | hard caps outside the app |

Token usage per agent and per investigation is recorded in every result and report, and
`aiops doctor` pings with a single output token.

## Troubleshooting

| Message | Fix |
|---------|-----|
| `llm.api_key is not set (ANTHROPIC_API_KEY)` | set the variable the profile references |
| `no AWS region (AWS_REGION, ...)` | `export AWS_REGION=eu-central-1` or set it in the AWS profile |
| `bedrock: no AWS credentials found` | `aws sso login --profile ...`, IRSA, or an instance role |
| `azure_openai: HTTP 404 ...` | the deployment name or `api_version` is wrong |
| `HTTP 400 ... tool_choice` | the model rejects forced tool use: `forced_tool_choice: false` |
| `rate limited after retries` | free-tier limits: use a smaller `fast` model, fewer agents, or raise the provider quota |
