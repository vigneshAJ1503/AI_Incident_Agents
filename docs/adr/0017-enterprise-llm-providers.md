# ADR-0017: Enterprise LLM providers behind one `LLMProvider` protocol

- **Status:** Accepted
- **Date:** 2026-09-29
- **PR:** PR-P4b (docs/setup/llm-providers.md)

## Context
Companies usually mandate one LLM platform: the Anthropic API, Amazon Bedrock or Azure
OpenAI, sometimes a self-hosted inference server. Until now only `openai_compat` existed,
so adopting the product meant a gateway in front of the approved platform or code changes.
The portability goal (ADR-0011/0012) says a new company is a new profile, never agent
code. Every agent, the planner and the RCA agent use two things only: `LLMProvider.generate`
(messages, tools, `tool_choice` auto/required/none, a model *role*) and
`generate_structured`, which forces a `submit` tool whose parameters are the schema.

## Decision
1. **One adapter per platform in `backend/src/aiops/llm/`**, selected by `llm.provider`
   (`openai_compat | anthropic | bedrock | azure_openai | fake`), built lazily by
   `llm.factory.create_provider` so a profile loads only its SDK. Each adapter maps the
   neutral types to the vendor's wire format and back: system prompt, assistant tool calls,
   tool results (merged into one user turn where the vendor requires it), `tool_choice`
   (`required` -> Anthropic `any` / Converse `any` / OpenAI `required`), usage (cache
   tokens count as input), finish reason. The `submit` pattern works unchanged.
2. **Anthropic: the official `anthropic` SDK (1.9.0, pinned)**, `AsyncAnthropic` with the
   project's `httpx2` client. Its built-in retries (408/409/429/5xx/529 overloaded,
   exponential backoff, `retry-after`) replace a hand-written loop. SDK 1.x has no sampling
   parameters; an explicit `llm.temperature` is sent via `extra_body` for older models.
3. **Bedrock: the Converse API via `boto3` (1.43.103, pinned), not the Anthropic SDK's
   Bedrock client and not `aioboto3`.** Converse is one request shape with tool use for
   every Bedrock model family (Claude, Nova, Llama, Mistral), which is what "the approved
   platform is Bedrock" means in practice; the Anthropic Bedrock clients speak Claude only.
   `aioboto3`/`aiobotocore` pin exact botocore versions and would fight other packages;
   the synchronous client is thread-safe, so calls run in `asyncio.to_thread` (agent calls
   are few and long, so a worker thread per call costs nothing that matters).
   botocore "standard" retries handle throttling and 5xx.
4. **Credentials:** keys only as `${VAR}` in the profile, and masked in every error and log
   line the adapters produce (tests assert it with SDK/botocore DEBUG logging on).
   **Bedrock takes no key at all:** region and credentials come from the standard AWS chain
   (env, `AWS_PROFILE`/SSO, IRSA, instance/task role); `llm.api_key` is a validation error
   for `bedrock`. Azure OpenAI uses an API key for now (Entra ID tokens: future work).
5. **Azure OpenAI reuses the OpenAI adapter** (`AsyncAzureOpenAI`: endpoint +
   `api_version`; roles map to deployment names), so request/response code is shared.
6. **Readiness is one function**, `llm.factory.config_problems` (`LLMConfig.missing()` +
   the AWS region): `/health` `configured`, `aiops llm ping`, `aiops doctor` (provider,
   endpoint and model per role) and live evals all say which field and which env var is
   missing.
7. **`forced_tool_choice: false`** sends `required` as `auto` for models that reject forced
   tool use (some Bedrock models; newer Claude models reject `any`); `generate_structured`
   then relies on its instruction + corrective retry.
8. **Tests never call a vendor:** the real SDKs run against mocked transports
   (`httpx2.MockTransport`; a botocore `before-send` hook, so SigV4 signing and the retry
   handler still run), including a full orchestrated replay investigation per provider.

## Consequences
- Switching platforms = edit the `llm` block of the profile (the `_template` has one per
  provider) and set the env vars; agents, prompts, the orchestrator and evals are untouched.
- Two new pinned runtime dependencies (`anthropic`, `boto3`, ~15 MB installed); both are
  imported only when selected.
- No live verification on the dev machine (no keys); the wire formats are covered by the
  mocked-transport tests and should be smoke-tested with `aiops llm ping` at the company.
- Model ids are never hard-coded; docs give examples only.

## Alternatives considered
- **A LiteLLM gateway in front of everything (`openai_compat` only):** one more service to
  run and secure, and many enterprises disallow a third-party proxy for their LLM traffic.
  Still supported: point `openai_compat` at the company's gateway.
- **LangChain chat models:** a large dependency for a thin mapping we already own, and its
  own tool-calling abstractions would leak into agents.
- **Anthropic SDK Bedrock client for Bedrock:** simpler for Claude-only shops, but Claude
  only; can be added later as an option if a company needs Messages-API-only features.
