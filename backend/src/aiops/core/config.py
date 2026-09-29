"""Company profiles: YAML + ${ENV} interpolation, validated with Pydantic.

Agents never talk to a vendor directly. They ask for a *capability* (``logs``,
``metrics``, ...) and the profile binds that capability to a provider and an MCP
server. Onboarding a new company = a new ``profiles/<company>/`` folder, selected with
``AIOPS_PROFILE`` (ADR-0011, docs/portability.md)::

    profiles/<name>/profile.yaml    llm, capabilities, limits, guardrails, agents, metadata
    profiles/<name>/services.yaml   the service catalog (optional when ``extends:`` gives one)
    profiles/<name>/prompts/        optional prompt overrides (fall back to config/prompts/)
    profiles/<name>/.env.example    the ${VAR} names the profile needs (secrets live in .env)

Back-compat: ``AIOPS_ENV`` is a deprecated alias of ``AIOPS_PROFILE``, and a legacy
``config/environments/<name>.yaml`` (+ ``config/service-catalog/``) still loads when no
profile of that name exists.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from aiops.core.guardrails.redaction import PII_KINDS, SECRET_KINDS, SUPPORTED_KINDS

_log = logging.getLogger(__name__)

PROFILE_VAR = "AIOPS_PROFILE"
#: Deprecated alias of AIOPS_PROFILE (kept so existing shells, docs and scripts keep working).
ENV_VAR = "AIOPS_ENV"
CONFIG_DIR_VAR = "AIOPS_CONFIG_DIR"
#: Where profiles live (default: <repo>/profiles). Point it at a private repo/folder so a
#: company's profile never has to be committed to this public repository.
PROFILES_DIR_VAR = "AIOPS_PROFILES_DIR"
DEFAULT_ENV = "local"
PROFILE_FILE = "profile.yaml"
CATALOG_FILE = "services.yaml"

# ${VAR} (required) or ${VAR:-default} (optional, default may be empty)
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


#: An HTTP header name (RFC 9110 token, restricted to the usual letters/digits/dashes).
HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")


class ConfigError(Exception):
    """Configuration is missing or invalid. The message is meant for humans."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MCPServerConfig(_Strict):
    transport: Literal["http", "stdio"]
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = Field(default=30.0, gt=0)
    #: HTTP headers sent to an ``http`` MCP server that sits behind authentication, e.g.
    #: ``{Authorization: "Bearer ${ACME_LOGS_MCP_TOKEN}"}``. Values are secrets: masked in
    #: every dump, never logged. The Web UI stores them encrypted (PR-046, ``AIOPS_SECRETS_KEY``).
    headers: dict[str, SecretStr] = Field(default_factory=dict)

    @field_validator("headers")
    @classmethod
    def _valid_headers(cls, value: dict[str, SecretStr]) -> dict[str, SecretStr]:
        for name, secret in value.items():
            if not HEADER_NAME.match(name):
                raise ValueError(f"invalid header name {name!r} (letters, digits and '-')")
            if any(c in secret.get_secret_value() for c in "\r\n\0"):
                raise ValueError(f"header {name}: the value must be a single line")
        return value

    @model_validator(mode="after")
    def _check_transport(self) -> MCPServerConfig:
        if self.transport == "http" and not self.url:
            raise ValueError("transport 'http' requires 'url'")
        if self.transport == "stdio" and not self.command:
            raise ValueError("transport 'stdio' requires 'command'")
        if self.transport == "stdio" and self.headers:
            raise ValueError("'headers' are sent to http MCP servers only (transport is stdio)")
        return self

    def header_values(self) -> dict[str, str]:
        """The headers with their secret values, for the HTTP client only."""
        return {k: v.get_secret_value() for k, v in self.headers.items() if v.get_secret_value()}


class CapabilityLimits(_Strict):
    max_results: int = Field(default=500, gt=0)
    max_time_range_hours: int = Field(default=168, gt=0)
    query_timeout_s: float = Field(default=30.0, gt=0)
    #: Circuit breaker (PR-042): after this many consecutive failures (connect errors,
    #: timeouts, transport errors) the capability is skipped for ``breaker_reset_s``; then
    #: ONE probe is let through (half-open). Skipped = a PARTIAL evidence gap, not a timeout.
    breaker_failures: int = Field(default=3, ge=1)
    breaker_reset_s: float = Field(default=30.0, gt=0)


class CapabilityConfig(_Strict):
    provider: str
    enabled: bool = True
    mcp: MCPServerConfig
    tool_allowlist: list[str] = Field(min_length=1)
    #: Write tools. Never given to agents: only the approval executor may call them, and
    #: only for an APPROVED action proposal (core/guardrails/approvals.py).
    write_allowlist: list[str] = Field(default_factory=list)
    limits: CapabilityLimits = Field(default_factory=CapabilityLimits)
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _writes_are_not_readable(self) -> CapabilityConfig:
        both = sorted(set(self.tool_allowlist) & set(self.write_allowlist))
        if both:
            raise ValueError(
                f"tools {both} are in both tool_allowlist and write_allowlist; write tools "
                "must never be available to agents"
            )
        return self


ModelRole = Literal["fast", "agent", "rca"]
LLMProviderName = Literal["openai_compat", "anthropic", "bedrock", "azure_openai", "fake"]

#: The conventional env var behind each provider field, for readable error messages (the
#: profile YAML decides the real mapping via ``${VAR}``). Bedrock has no key: the standard
#: AWS credential chain (env, ~/.aws profile/SSO, IRSA, instance role) is used.
LLM_ENV_VARS: dict[str, dict[str, str]] = {
    "openai_compat": {"base_url": "OPENAI_COMPAT_BASE_URL", "api_key": "OPENAI_COMPAT_API_KEY"},
    "anthropic": {"base_url": "ANTHROPIC_BASE_URL", "api_key": "ANTHROPIC_API_KEY"},
    "azure_openai": {
        "base_url": "AZURE_OPENAI_ENDPOINT",
        "api_key": "AZURE_OPENAI_API_KEY",
        "api_version": "AZURE_OPENAI_API_VERSION",
    },
    "bedrock": {"base_url": "BEDROCK_ENDPOINT_URL", "region": "AWS_REGION"},
    "fake": {},
}


class LLMConfig(_Strict):
    """The hosted LLM. ``provider`` picks the adapter (``aiops.llm.factory``):

    - ``openai_compat``: any OpenAI-compatible Chat Completions API (Groq, Gemini, OpenAI,
      OpenRouter, a company vLLM/LiteLLM/Ollama *server*). Needs ``base_url`` + ``api_key``.
    - ``anthropic``: the Anthropic Messages API. Needs ``api_key``; ``base_url`` optional
      (an approved gateway/proxy).
    - ``bedrock``: the Amazon Bedrock Converse API. No key in YAML: region + credentials
      come from the standard AWS chain; ``base_url`` optional (a VPC/PrivateLink endpoint).
    - ``azure_openai``: an Azure OpenAI resource. ``base_url`` = the resource endpoint,
      ``api_version`` is required, and ``models`` map roles to *deployment* names.
    - ``fake``: scripted, zero tokens (tests, replay).
    """

    provider: LLMProviderName
    base_url: str | None = None
    api_key: SecretStr | None = None
    #: Azure OpenAI ``api-version`` query parameter (e.g. ``2024-10-21``).
    api_version: str | None = None
    #: Extra keys for the same provider; used in turn when one is rate-limited (free tiers).
    fallback_api_keys: list[SecretStr] = Field(default_factory=list)
    #: Max LLM requests in flight at once (0 = unlimited). Free tiers cap tokens per
    #: minute, so a small number (e.g. 2) smooths bursts from agents running in parallel.
    max_concurrent_requests: int = Field(default=0, ge=0)
    models: dict[ModelRole, str] = Field(default_factory=dict)
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=4, ge=0)
    #: Always sent to OpenAI-style APIs; sent to Anthropic/Bedrock only when set explicitly
    #: in the profile (recent Claude models reject sampling parameters).
    temperature: float = Field(default=0.0, ge=0, le=2)
    #: ``tool_choice="required"`` (the structured-output ``submit`` tool) is sent as a forced
    #: tool choice. Set false for models that reject forced tool use: ``required`` is then
    #: sent as ``auto`` and ``generate_structured`` relies on its prompt + corrective retry.
    forced_tool_choice: bool = True

    @model_validator(mode="after")
    def _provider_fields(self) -> LLMConfig:
        if self.provider == "bedrock" and self.api_key is not None:
            raise ValueError(
                "llm.api_key must not be set for provider 'bedrock': AWS credentials come from "
                "the standard AWS chain (AWS_PROFILE, SSO, IRSA, instance role), never YAML"
            )
        if self.api_version and self.provider != "azure_openai":
            raise ValueError("llm.api_version is only used by provider 'azure_openai'")
        return self

    @property
    def explicit_temperature(self) -> float | None:
        """``temperature`` only if the profile set it (for APIs that may reject it)."""
        return self.temperature if "temperature" in self.model_fields_set else None

    def env_var(self, field: str) -> str:
        """The conventional env var of ``field`` for this provider ('' if none)."""
        return LLM_ENV_VARS.get(self.provider, {}).get(field, "")

    def missing(self) -> list[str]:
        """Readable problems that stop real LLM calls, without network or SDK imports, e.g.
        ``["llm.api_key is not set (ANTHROPIC_API_KEY)"]``. Empty = ready to try."""
        if self.provider == "fake":
            return []
        problems: list[str] = []

        def need(field: str, value: str | None) -> None:
            if value is None or not value.strip():
                var = self.env_var(field)
                problems.append(f"llm.{field} is not set" + (f" ({var})" if var else ""))

        if self.provider in ("openai_compat", "azure_openai"):
            need("base_url", self.base_url)
        if self.provider == "azure_openai":
            need("api_version", self.api_version)
        if self.provider != "bedrock":
            need("api_key", self.api_key.get_secret_value() if self.api_key else None)
        if not self.models.get("agent"):
            what = "deployment" if self.provider == "azure_openai" else "model"
            problems.append(f"llm.models.agent ({what} name) is not set (LLM_MODEL_AGENT)")
        return problems

    def models_by_role(self) -> dict[str, str]:
        """``{"fast": ..., "agent": ..., "rca": ...}`` after the ``agent`` fallback."""
        agent = self.models.get("agent") or "-"
        return {role: self.models.get(role) or agent for role in ("fast", "agent", "rca")}

    #: Provider-specific request parameters passed through as-is,
    #: e.g. {"reasoning_effort": "low"} for reasoning models on Groq.
    extra: dict[str, Any] = Field(default_factory=dict)

    def model_for(self, role: ModelRole) -> str:
        """Model for a role, falling back to ``agent`` when a role is not set."""
        model = self.models.get(role) or self.models.get("agent")
        if not model:
            raise ConfigError(
                f"No LLM model configured for role '{role}'. "
                "Set LLM_MODEL_AGENT (and optionally LLM_MODEL_FAST / LLM_MODEL_RCA) in .env."
            )
        return model


class AgentLimits(_Strict):
    max_steps: int = Field(default=8, gt=0)
    max_tool_calls: int = Field(default=10, gt=0)
    max_execution_s: float = Field(default=60.0, gt=0)
    max_tokens: int = Field(default=60_000, gt=0)


class AgentConfig(_Strict):
    enabled: bool = True
    prompt_version: str | None = None  # None = latest
    model_role: ModelRole = "agent"
    limits: AgentLimits | None = None  # None = global limits


class GuardrailsConfig(_Strict):
    read_only: bool = True
    #: Redaction kinds (core/guardrails/redaction.py). Groups: ``secrets`` = every secret
    #: kind, ``pii`` = emails + credit cards + IPs. Default: every secret kind, no PII.
    redact: list[str] = Field(default_factory=lambda: list(SECRET_KINDS))
    max_tool_output_chars: int = Field(default=8_000, gt=0)
    audit_log_path: str = ".data/audit.jsonl"
    #: Human approvals for write actions (PR-014). JSON file store until Postgres (PR-032).
    approvals_path: str = ".data/approvals.json"
    approvals_audit_path: str = ".data/approvals-audit.jsonl"
    approval_ttl_hours: float = Field(default=24.0, gt=0)

    @field_validator("redact", mode="before")
    @classmethod
    def _expand_groups(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if not isinstance(value, list):
            return value
        groups = {"secrets": SECRET_KINDS, "pii": PII_KINDS}
        expanded = [k for v in value for k in groups.get(str(v), (str(v),))]
        unknown = sorted(set(expanded) - SUPPORTED_KINDS)
        if unknown:
            raise ValueError(
                f"unknown redaction kinds {unknown}; supported: {sorted(SUPPORTED_KINDS)} "
                "(groups: secrets, pii)"
            )
        return list(dict.fromkeys(expanded))


class FollowupRule(_Strict):
    """Gap analysis (round 2): a round-1 signal -> a targeted follow-up step."""

    agent: str
    objective: str
    #: ``incident`` = the investigated service; ``dependency`` = the catalog dependency
    #: named by round-1 evidence (e.g. the slow downstream service).
    target: Literal["incident", "dependency"] = "incident"
    keywords: list[str] = Field(default_factory=list)  # extra search terms (knowledge)


class SeverityRules(_Strict):
    """How the response builder rates an investigation's severity (the ONE place).

    First match wins:

    * ``none``: no problem signal at all;
    * ``critical``: users get errors on a service whose catalog ``tier`` is in
      ``critical_tiers``, with a root cause at least ``critical_min_confidence`` confident.
      "Users get errors" = the measured peak error ratio of the service (metric evidence
      ``error_rate``) is at least ``critical_error_rate``; without that measurement, an
      error signal;
    * ``high``: user-facing impact anywhere, or a critical alert firing;
    * ``medium``: latency only;
    * ``low``: anything else (e.g. a risky change without symptoms).
    """

    critical_tiers: list[int] = Field(default_factory=lambda: [1])
    #: Tier of catalog services that set none.
    default_tier: int = Field(default=2, ge=1)
    critical_min_confidence: float = Field(default=0.7, ge=0, le=1)
    critical_error_rate: float = Field(default=0.05, ge=0, le=1)
    error_signals: list[str] = Field(
        default_factory=lambda: [
            "error_rate_up",
            "db_timeout_errors_up",
            "new_error_pattern",
            "oom_errors",
        ]
    )
    availability_signals: list[str] = Field(
        default_factory=lambda: [
            "image_pull_error",
            "replicas_unavailable",
            "cache_down",
            "oom_killed",
            "crash_loop",
        ]
    )
    latency_signals: list[str] = Field(
        default_factory=lambda: ["latency_up", "dependency_latency_up", "dependency_timeouts"]
    )
    critical_alert_signal: str = "critical_alert_firing"


class OrchestratorConfig(_Strict):
    """How investigations are planned and executed (defaults suit the local stack)."""

    default_window: str = "30m"
    max_concurrency: int = Field(default=4, gt=0)
    #: Wall-clock cap per step; default = the agent's max_execution_s + 15 s.
    step_timeout_s: float | None = Field(default=None, gt=0)
    max_rounds: int = Field(default=2, ge=1, le=2)
    #: Token budget of ONE investigation (agents + planner + RCA). Steps that would start
    #: after it is spent are skipped and the investigation becomes PARTIAL; agents already
    #: running finish with their deterministic analysis (PR-041).
    max_tokens: int = Field(default=200_000, gt=0)
    #: Cost budget of ONE investigation in USD, from the ``cost.pricing`` table (PR-041).
    #: Same effect as ``max_tokens`` once spent. 0 = no cost cap (the token budget still
    #: applies); with the free tiers' $0 prices it never triggers.
    max_cost_usd: float = Field(default=0.0, ge=0)
    #: Identical read-only tool calls (same capability, tool and arguments) within ONE
    #: investigation are answered from a short-lived cache for this long (PR-041). 0 = off.
    tool_cache_ttl_s: float = Field(default=120.0, ge=0)
    #: Most cached tool results kept per API process (least recently used are evicted).
    tool_cache_max_entries: int = Field(default=512, gt=0)
    #: Agents that need round-1 findings as input (hints/symptoms) run in round 2.
    round2_agents: list[str] = Field(default_factory=lambda: ["knowledge", "tickets"])
    #: Extra/overriding gap-analysis rules: signal -> follow-ups (merged over the defaults).
    followups: dict[str, list[FollowupRule]] = Field(default_factory=dict)
    #: Use the LLM ("fast" role) when deterministic parsing can't find a service.
    llm_planner_fallback: bool = True
    #: Let the LLM ("rca" role) rank and phrase the deterministic hypotheses.
    llm_rca: bool = True
    #: Report severity rules (response builder).
    severity: SeverityRules = Field(default_factory=SeverityRules)


class ModelPrice(_Strict):
    """USD per million tokens."""

    input: float = Field(default=0.0, ge=0)
    output: float = Field(default=0.0, ge=0)


class CostConfig(_Strict):
    """LLM cost estimates (PR-041, docs/observability.md): per call, agent, investigation."""

    #: USD per 1M tokens. Keys, most specific first: the model id
    #: (``llama-3.3-70b-versatile``), the provider host (``api.groq.com``), ``*``.
    #: Unlisted = 0 (the free tiers this project uses). Override per company profile.
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)


class TracingConfig(_Strict):
    """OpenTelemetry spans: investigation -> agent -> tool call / LLM call (PR-041)."""

    #: OTLP/HTTP endpoint, e.g. ``http://localhost:4318`` (``/v1/traces`` is appended).
    #: Unset = tracing off (a no-op tracer, nothing is exported). Falls back to the standard
    #: ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var.
    otlp_endpoint: str | None = None
    service_name: str = "aiops"
    #: Fraction of investigations traced (parent-based: a trace is kept or dropped whole).
    sample_ratio: float = Field(default=1.0, ge=0, le=1)

    @field_validator("otlp_endpoint")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is not None and not re.match(r"^https?://[^\s]+$", value.strip()):
            raise ValueError(f"otlp_endpoint must be an http(s) URL, got {value!r}")
        return value.strip() if value else None


class MetricsConfig(_Strict):
    """Prometheus metrics on the API's ``/metrics`` (PR-041)."""

    enabled: bool = True


class ObservabilityConfig(_Strict):
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)


class EvalsConfig(_Strict):
    """The system evaluation (PR-040, ``aiops evaluate``, docs/evals.md)."""

    #: Deprecated (PR-041): use ``cost.pricing``. Still read; ``cost.pricing`` wins per key.
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)
    #: Also score root causes with an LLM judge (needs a hosted LLM key; skipped otherwise).
    llm_judge: bool = False
    #: Regression gate: tolerated drop/rise of a rate before the run fails.
    tolerance: float = Field(default=0.0, ge=0, le=1)


class StorageConfig(_Strict):
    """The evidence store (PR-032): investigations, events, approvals, audit."""

    #: SQLAlchemy URL. Unset = ``$AIOPS_DATABASE_URL``, else the local compose Postgres from
    #: POSTGRES_* (``postgresql+psycopg://aiops:...@localhost:15432/aiops``).
    database_url: str | None = None
    db_schema: str = "investigations"  # Postgres schema (ignored by SQLite)
    #: Where approvals and the tool-call audit live. ``file``/``jsonl`` = the JSON files in
    #: ``guardrails`` (the fallback); ``postgres`` = the store (falls back to JSONL on errors).
    approvals: Literal["file", "postgres"] = "file"
    audit: Literal["jsonl", "postgres"] = "jsonl"


_RATE = re.compile(r"^\s*(\d+)\s*/\s*(second|minute|hour|s|m|h)\s*$")
_RATE_UNITS = {"second": 1, "s": 1, "minute": 60, "m": 60, "hour": 3600, "h": 3600}
RATE_LIMIT_GROUPS = frozenset({"investigations", "approvals", "scenarios", "integrations"})


def parse_rate(value: str) -> tuple[int, float]:
    """``"10/minute"`` -> ``(10, 60.0)`` (requests, per seconds)."""
    match = _RATE.match(value)
    if not match:
        raise ValueError(f"invalid rate {value!r}: use e.g. '10/minute', '5/s', '100/hour'")
    return int(match.group(1)), float(_RATE_UNITS[match.group(2)])


class OIDCConfig(_Strict):
    """OIDC bearer tokens (PR-045: multi-tenancy + RBAC + SSO). Declared now so profiles can
    be written; ``api.auth: oidc`` is refused until the token validator lands."""

    issuer: str
    audience: str
    jwks_url: str | None = None  # default: <issuer>/.well-known/jwks.json
    subject_claim: str = "sub"
    roles_claim: str = "roles"


class ApiConfig(_Strict):
    """The REST + SSE API (PR-035, docs/api/README.md). Defaults suit the local stack."""

    #: Browser origins allowed by CORS (the Web UI). Comma-separated in env vars.
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3100",
            "http://127.0.0.1:3100",
        ]
    )
    #: Optional shared secret: when set, every /api route except /api/health requires the
    #: ``X-API-Key`` header (SSE also accepts ``?api_key=``, EventSource can't set headers).
    api_key: SecretStr | None = None
    #: Named keys (``{name: key}``; env: ``"alice:KEY,bob:KEY"``). A named key is an
    #: authenticated identity: approvals are recorded as decided by that name.
    api_keys: dict[str, SecretStr] = Field(default_factory=dict)
    #: ``auto`` = ``api_key`` when a key is configured, else ``none``; ``oidc`` = PR-045.
    auth: Literal["auto", "none", "api_key", "oidc"] = "auto"
    oidc: OIDCConfig | None = None
    #: Per-client limits (client = authenticated name, else the peer IP): ``N/second|
    #: minute|hour``. ``investigations`` = POST /investigations + clarify; ``approvals`` =
    #: approve/deny/ticket drafts; ``scenarios`` = fault inject/revert; ``integrations`` =
    #: saving an integration and "test connection" (PR-046).
    rate_limits: dict[str, str] = Field(
        default_factory=lambda: {
            "investigations": "10/minute",
            "approvals": "30/minute",
            "scenarios": "6/minute",
            "integrations": "30/minute",
        }
    )
    #: Largest accepted request body (HTTP 413 above).
    max_body_bytes: int = Field(default=65_536, gt=0)
    #: How long an ``Idempotency-Key`` of POST /investigations is remembered.
    idempotency_ttl_s: float = Field(default=86_400.0, gt=0)
    #: Stuck-investigation reaper: ``running``/``pending`` with no event for this long and
    #: not running in this process -> ``failed`` (reason in an ``error`` event). 0 = off.
    stuck_after_s: float = Field(default=900.0, ge=0)
    reaper_interval_s: float = Field(default=60.0, gt=0)
    #: Investigations running at the same time in one API process (more -> HTTP 429).
    max_running_investigations: int = Field(default=2, gt=0)
    #: SSE keep-alive interval.
    heartbeat_s: float = Field(default=15.0, gt=0)
    #: ``GET /health`` caches capability reachability (cheap TCP checks) this long.
    health_cache_s: float = Field(default=30.0, ge=0)
    #: Timeout of one capability reachability check.
    health_timeout_s: float = Field(default=1.0, gt=0)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [o.strip() for o in value.split(",") if o.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard(cls, value: list[str]) -> list[str]:
        bad = [o for o in value if o == "*" or not re.match(r"^https?://[^/\s*]+$", o)]
        if bad:
            raise ValueError(
                f"cors_origins must be exact origins like http://localhost:3100 (no '*', no "
                f"path): {bad}"
            )
        return value

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_keys(cls, value: Any) -> Any:
        if isinstance(value, str):
            parsed = {}
            for pair in (p.strip() for p in value.split(",")):
                if not pair:
                    continue
                name, sep, key = pair.partition(":")
                if not sep or not name.strip() or len(key.strip()) < 16:
                    raise ValueError(
                        "api_keys: use 'name:KEY,name2:KEY2' with keys of 16+ characters"
                    )
                parsed[name.strip()] = key.strip()
            return parsed
        return value

    @field_validator("rate_limits")
    @classmethod
    def _valid_rates(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - RATE_LIMIT_GROUPS)
        if unknown:
            raise ValueError(f"unknown rate limit groups {unknown}: {sorted(RATE_LIMIT_GROUPS)}")
        for rate in value.values():
            parse_rate(rate)
        return value

    @model_validator(mode="after")
    def _auth_mode(self) -> ApiConfig:
        if self.auth == "oidc":
            raise ValueError(
                "api.auth 'oidc' is reserved for PR-045 (multi-tenancy + RBAC + SSO); use "
                "'api_key' with api.api_keys for named identities until then"
            )
        if self.auth == "api_key" and not self.key_auth_configured:
            raise ValueError("api.auth 'api_key' needs api.api_key or api.api_keys")
        return self

    @property
    def key_auth_configured(self) -> bool:
        shared = self.api_key is not None and bool(self.api_key.get_secret_value())
        return shared or any(k.get_secret_value() for k in self.api_keys.values())

    @property
    def auth_mode(self) -> Literal["none", "api_key", "oidc"]:
        if self.auth == "auto":
            return "api_key" if self.key_auth_configured else "none"
        return self.auth


class ProfileMetadata(_Strict):
    """Who this profile is for. Informational; never inherited through ``extends``."""

    company: str = ""
    description: str = ""
    owners: list[str] = Field(default_factory=list)


class Settings(_Strict):
    environment: str  # the profile name (legacy: the environment file name)
    metadata: ProfileMetadata = Field(default_factory=ProfileMetadata)
    llm: LLMConfig
    capabilities: dict[str, CapabilityConfig] = Field(default_factory=dict)
    limits: AgentLimits = Field(default_factory=AgentLimits)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    evals: EvalsConfig = Field(default_factory=EvalsConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    service_catalog: str = "local"

    # Set by the loader, not by YAML.
    config_dir: Path = Field(default=Path("config"), exclude=True)
    #: Profile folders, the selected one first, then its ``extends`` ancestors. Empty for a
    #: legacy ``config/environments/*.yaml`` environment.
    profile_chain: tuple[Path, ...] = Field(default=(), exclude=True)

    @property
    def profile(self) -> str:
        return self.environment

    @property
    def profile_dir(self) -> Path | None:
        return self.profile_chain[0] if self.profile_chain else None

    def catalog_path(self) -> Path:
        """The service catalog: ``profiles/<service_catalog>/services.yaml`` (legacy:
        ``config/service-catalog/<service_catalog>.yaml``)."""
        if self.profile_chain:
            return self.profile_chain[0].parent / self.service_catalog / CATALOG_FILE
        return self.config_dir / "service-catalog" / f"{self.service_catalog}.yaml"

    def prompt_override_dirs(self) -> list[Path]:
        """``prompts/`` folders of the profile chain (most specific first) that exist."""
        return [d / "prompts" for d in self.profile_chain if (d / "prompts").is_dir()]

    def capability(self, name: str) -> CapabilityConfig:
        cap = self.capabilities.get(name)
        if cap is None or not cap.enabled:
            configured = ", ".join(sorted(self.capabilities)) or "none"
            raise ConfigError(
                f"Capability '{name}' is not configured/enabled in environment "
                f"'{self.environment}' (configured: {configured})."
            )
        return cap

    def agent(self, name: str) -> AgentConfig:
        return self.agents.get(name, AgentConfig())

    def agent_limits(self, name: str) -> AgentLimits:
        return self.agent(name).limits or self.limits

    def pricing(self) -> dict[str, ModelPrice]:
        """The price table: ``cost.pricing`` over the deprecated ``evals.pricing``."""
        return {**self.evals.pricing, **self.cost.pricing}

    def repo_path(self, path: str) -> Path:
        """A configured path (e.g. '.data/approvals.json'): relative = relative to the repo."""
        resolved = Path(path)
        return resolved if resolved.is_absolute() else self.config_dir.parent / resolved

    def safe_dump(self) -> dict[str, Any]:
        """Dump for display: secrets are masked by SecretStr."""
        return self.model_dump(mode="json")


# --------------------------------------------------------------------------- loading


def _is_config_dir(path: Path) -> bool:
    """The shared ``config/`` dir: prompts (current layout) or environments (legacy)."""
    return (path / "prompts").is_dir() or (path / "environments").is_dir()


def find_config_dir(start: Path | None = None) -> Path:
    """Locate the repo ``config/`` directory (env override, else walk upwards)."""
    override = os.environ.get(CONFIG_DIR_VAR)
    if override:
        path = Path(override).expanduser().resolve()
        if not _is_config_dir(path):
            raise ConfigError(
                f"{CONFIG_DIR_VAR}={override} has neither 'prompts/' nor 'environments/'."
            )
        return path
    candidates = [start or Path.cwd(), Path(__file__).resolve()]
    for origin in candidates:
        for parent in [origin, *origin.parents]:
            if _is_config_dir(parent / "config"):
                return parent / "config"
    raise ConfigError(
        "Could not find the repository 'config/' directory. Run from the repository or set "
        f"{CONFIG_DIR_VAR}."
    )


def find_profiles_dir(config_dir: Path) -> Path:
    """``$AIOPS_PROFILES_DIR``, else ``profiles/`` next to ``config/``."""
    override = os.environ.get(PROFILES_DIR_VAR)
    return Path(override).expanduser().resolve() if override else config_dir.parent / "profiles"


def list_profile_names(profiles_dir: Path) -> list[str]:
    if not profiles_dir.is_dir():
        return []
    return sorted(p.name for p in profiles_dir.iterdir() if (p / PROFILE_FILE).is_file())


_warned_env_alias = False


def selected_profile(explicit: str | None = None) -> str:
    """``explicit`` > ``$AIOPS_PROFILE`` > ``$AIOPS_ENV`` (deprecated) > ``local``."""
    global _warned_env_alias
    if explicit:
        return explicit
    profile, legacy = os.environ.get(PROFILE_VAR), os.environ.get(ENV_VAR)
    if profile:
        return profile
    if legacy:
        if not _warned_env_alias:
            _warned_env_alias = True
            _log.warning(
                "%s is deprecated; use %s=%s (same meaning, docs/portability.md).",
                ENV_VAR,
                PROFILE_VAR,
                legacy,
            )
        return legacy
    return DEFAULT_ENV


def interpolate(value: Any, missing: list[str], *, keep_missing: bool = False) -> Any:
    """Recursively replace ${VAR} / ${VAR:-default} in strings; collect missing vars.

    ``keep_missing`` leaves an unset required ``${VAR}`` in place (for display/diff).
    """
    if isinstance(value, str):

        def _sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            env_value = os.environ.get(name)
            if env_value not in (None, ""):
                return env_value
            if default is not None:
                return default
            missing.append(name)
            return match.group(0) if keep_missing else ""

        return _INTERPOLATION.sub(_sub, value)
    if isinstance(value, dict):
        return {k: interpolate(v, missing, keep_missing=keep_missing) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, missing, keep_missing=keep_missing) for v in value]
    return value


def referenced_env_vars(value: Any) -> dict[str, bool]:
    """``{VAR: required}`` for every ${VAR} / ${VAR:-default} referenced in ``value``."""
    found: dict[str, bool] = {}

    def _add(name: str, required: bool) -> None:
        found[name] = found.get(name, False) or required

    if isinstance(value, str):
        for match in _INTERPOLATION.finditer(value):
            _add(match.group(1), match.group(2) is None)
    elif isinstance(value, dict | list):
        for item in value.values() if isinstance(value, dict) else value:
            for name, required in referenced_env_vars(item).items():
                _add(name, required)
    return found


def _drop_empty(value: Any) -> Any:
    """Treat empty strings (e.g. unset optional env vars) as 'not provided'."""
    if isinstance(value, dict):
        return {k: _drop_empty(v) for k, v in value.items() if v != ""}
    if isinstance(value, list):
        return [_drop_empty(v) for v in value if v != ""]
    return value


def format_validation_error(err: ValidationError, source: Path) -> str:
    lines = [f"Invalid configuration in {source}:"]
    for issue in err.errors():
        location = ".".join(str(part) for part in issue["loc"]) or "<root>"
        lines.append(f"  - {location}: {issue['msg']}")
    return "\n".join(lines)


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML syntax error in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level.")
    return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """``override`` on top of ``base``: mappings merge recursively, anything else replaces."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _config_name(path: Path) -> str:
    """'profiles/acme/profile.yaml' -> 'acme'; 'environments/local.yaml' -> 'local'."""
    return path.parent.name if path.name in (PROFILE_FILE, CATALOG_FILE) else path.stem


def extends_path(path: Path, parent_name: str) -> Path:
    """Where ``extends: <parent_name>`` points: the same file of profile ``<parent_name>``
    (``profiles/<parent>/profile.yaml``), or a sibling file in the legacy layout."""
    if path.name in (PROFILE_FILE, CATALOG_FILE):
        return path.parent.parent / parent_name / path.name
    return path.parent / f"{parent_name}.yaml"


def load_extending_yaml(
    path: Path,
    merge: Any = deep_merge,
    *,
    drop: tuple[str, ...] = (),
    _seen: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Load ``path``; ``extends: <name>`` = another profile (or sibling file) it overrides.

    Chains are allowed. ``drop`` = keys of the parent that a child never inherits
    (e.g. ``environment``).
    """
    if path in _seen:
        chain = " -> ".join(_config_name(p) for p in (*_seen, path))
        raise ConfigError(f"Circular 'extends' in {path.parent}: {chain}")
    data = load_yaml(path)
    parent_name = data.pop("extends", None)
    if parent_name is None:
        return data
    if not isinstance(parent_name, str) or not parent_name.strip():
        raise ConfigError(f"{path}: 'extends' must be the name of another profile")
    parent_path = extends_path(path, parent_name.strip())
    parent = load_extending_yaml(parent_path, merge, drop=drop, _seen=(*_seen, path))
    for key in drop:
        parent.pop(key, None)
    result: dict[str, Any] = merge(parent, data)
    return result


def profile_chain(profile_dir: Path) -> tuple[Path, ...]:
    """``profile_dir`` followed by its ``extends`` ancestors."""
    chain: list[Path] = []
    current: Path | None = profile_dir
    while current is not None:
        if current in chain:
            names = " -> ".join(p.name for p in (*chain, current))
            raise ConfigError(f"Circular 'extends' in {current.parent}: {names}")
        chain.append(current)
        parent = load_yaml(current / PROFILE_FILE).get("extends")
        current = current.parent / str(parent).strip() if parent else None
    return tuple(chain)


def _profile_not_found(name: str, profiles_dir: Path, config_dir: Path) -> ConfigError:
    available = list_profile_names(profiles_dir)
    legacy = sorted(p.stem for p in (config_dir / "environments").glob("*.yaml"))
    known = ", ".join([*available, *(f"{n} (legacy)" for n in legacy)]) or "none"
    return ConfigError(
        f"Profile '{name}' not found: no {profiles_dir / name / PROFILE_FILE} "
        f"(nor legacy {config_dir / 'environments' / f'{name}.yaml'}). Available: {known}. "
        f"Create one with: aiops profile init {name}"
    )


def load_settings_lenient(
    env: str | None = None, config_dir: Path | None = None, *, keep_missing: bool = False
) -> tuple[Settings, list[str]]:
    """Load a profile; returns ``(settings, missing required env vars)``.

    With ``keep_missing`` an unset required ``${VAR}`` stays literally in the value instead
    of failing (``aiops profile show/diff`` of a profile whose secrets aren't set here).
    """
    config_dir = config_dir or find_config_dir()
    name = selected_profile(env)
    profiles_dir = find_profiles_dir(config_dir)
    profile_dir = profiles_dir / name
    chain: tuple[Path, ...] = ()
    if (profile_dir / PROFILE_FILE).is_file():
        path = profile_dir / PROFILE_FILE
        chain = profile_chain(profile_dir)
        # The profile's own secrets file (gitignored), then the repo .env; the shell wins.
        load_dotenv(profile_dir / ".env", override=False)
    else:
        path = config_dir / "environments" / f"{name}.yaml"
        if not path.is_file():
            raise _profile_not_found(name, profiles_dir, config_dir)
    load_dotenv(config_dir.parent / ".env", override=False)

    missing: list[str] = []
    merged = load_extending_yaml(path, drop=("environment", "metadata"))
    raw = _drop_empty(interpolate(merged, missing, keep_missing=keep_missing))
    missing = sorted(set(missing))
    label = f"profile '{name}'" if chain else path.name
    if missing and not keep_missing:
        where = f"profiles/{name}/.env, the repo .env or the shell" if chain else ".env"
        raise ConfigError(
            f"Missing required environment variables for {label}: {', '.join(missing)} "
            f"(set them in {where}; see the profile's .env.example)"
        )
    raw.setdefault("environment", name)
    if chain and "service_catalog" not in raw:
        # The nearest profile in the chain that has a services.yaml.
        owner = next((d for d in chain if (d / CATALOG_FILE).is_file()), chain[0])
        raw["service_catalog"] = owner.name
    try:
        settings = Settings.model_validate(raw)
    except ValidationError as err:
        raise ConfigError(format_validation_error(err, path)) from err
    return settings.model_copy(update={"config_dir": config_dir, "profile_chain": chain}), missing


def load_settings(env: str | None = None, config_dir: Path | None = None) -> Settings:
    """Load and validate profile ``env`` (default: ``$AIOPS_PROFILE``, then the deprecated
    ``$AIOPS_ENV``, then ``local``). Fails fast with readable errors.

    A profile may start from another one with ``extends: <profile>`` and override only
    what differs (e.g. ``local-k8s`` = ``local`` with real Kubernetes logs).
    """
    return load_settings_lenient(env, config_dir)[0]


@lru_cache(maxsize=4)
def get_settings(env: str | None = None) -> Settings:
    return load_settings(env)
