"""Environment configuration: YAML + ${ENV} interpolation, validated with Pydantic.

Agents never talk to a vendor directly. They ask for a *capability* (``logs``,
``metrics``, ...) and the environment file binds that capability to a provider
and an MCP server. Onboarding a new company = a new ``config/environments/*.yaml``.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

ENV_VAR = "AIOPS_ENV"
CONFIG_DIR_VAR = "AIOPS_CONFIG_DIR"
DEFAULT_ENV = "local"

# ${VAR} (required) or ${VAR:-default} (optional, default may be empty)
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


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

    @model_validator(mode="after")
    def _check_transport(self) -> MCPServerConfig:
        if self.transport == "http" and not self.url:
            raise ValueError("transport 'http' requires 'url'")
        if self.transport == "stdio" and not self.command:
            raise ValueError("transport 'stdio' requires 'command'")
        return self


class CapabilityLimits(_Strict):
    max_results: int = Field(default=500, gt=0)
    max_time_range_hours: int = Field(default=168, gt=0)
    query_timeout_s: float = Field(default=30.0, gt=0)


class CapabilityConfig(_Strict):
    provider: str
    enabled: bool = True
    mcp: MCPServerConfig
    tool_allowlist: list[str] = Field(min_length=1)
    limits: CapabilityLimits = Field(default_factory=CapabilityLimits)
    settings: dict[str, Any] = Field(default_factory=dict)


ModelRole = Literal["fast", "agent", "rca"]


class LLMConfig(_Strict):
    provider: Literal["openai_compat", "fake"]
    base_url: str | None = None
    api_key: SecretStr | None = None
    models: dict[ModelRole, str] = Field(default_factory=dict)
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=4, ge=0)
    temperature: float = Field(default=0.0, ge=0, le=2)

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
    redact: list[str] = Field(default_factory=list)
    max_tool_output_chars: int = Field(default=8_000, gt=0)
    audit_log_path: str = ".data/audit.jsonl"


class Settings(_Strict):
    environment: str
    llm: LLMConfig
    capabilities: dict[str, CapabilityConfig] = Field(default_factory=dict)
    limits: AgentLimits = Field(default_factory=AgentLimits)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    service_catalog: str = "local"

    # Set by the loader, not by YAML.
    config_dir: Path = Field(default=Path("config"), exclude=True)

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

    def safe_dump(self) -> dict[str, Any]:
        """Dump for display: secrets are masked by SecretStr."""
        return self.model_dump(mode="json")


# --------------------------------------------------------------------------- loading


def find_config_dir(start: Path | None = None) -> Path:
    """Locate the repo ``config/`` directory (env override, else walk upwards)."""
    override = os.environ.get(CONFIG_DIR_VAR)
    if override:
        path = Path(override).expanduser().resolve()
        if not (path / "environments").is_dir():
            raise ConfigError(f"{CONFIG_DIR_VAR}={override} has no 'environments/' directory.")
        return path
    candidates = [start or Path.cwd(), Path(__file__).resolve()]
    for origin in candidates:
        for parent in [origin, *origin.parents]:
            if (parent / "config" / "environments").is_dir():
                return parent / "config"
    raise ConfigError(
        f"Could not find 'config/environments/'. Run from the repository or set {CONFIG_DIR_VAR}."
    )


def interpolate(value: Any, missing: list[str]) -> Any:
    """Recursively replace ${VAR} / ${VAR:-default} in strings; collect missing vars."""
    if isinstance(value, str):

        def _sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            env_value = os.environ.get(name)
            if env_value not in (None, ""):
                return env_value
            if default is not None:
                return default
            missing.append(name)
            return ""

        return _INTERPOLATION.sub(_sub, value)
    if isinstance(value, dict):
        return {k: interpolate(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, missing) for v in value]
    return value


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


def load_settings(env: str | None = None, config_dir: Path | None = None) -> Settings:
    """Load and validate ``config/environments/<env>.yaml``. Fails fast with readable errors."""
    config_dir = config_dir or find_config_dir()
    load_dotenv(config_dir.parent / ".env", override=False)
    env = env or os.environ.get(ENV_VAR) or DEFAULT_ENV
    path = config_dir / "environments" / f"{env}.yaml"

    missing: list[str] = []
    raw = _drop_empty(interpolate(load_yaml(path), missing))
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ConfigError(f"Missing required environment variables for {path.name}: {names}")
    raw.setdefault("environment", env)
    try:
        settings = Settings.model_validate(raw)
    except ValidationError as err:
        raise ConfigError(format_validation_error(err, path)) from err
    return settings.model_copy(update={"config_dir": config_dir})


@lru_cache(maxsize=4)
def get_settings(env: str | None = None) -> Settings:
    return load_settings(env)
