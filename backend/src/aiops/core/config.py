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
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

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
    #: Human approvals for write actions (PR-014). JSON file store until Postgres (PR-032).
    approvals_path: str = ".data/approvals.json"
    approvals_audit_path: str = ".data/approvals-audit.jsonl"
    approval_ttl_hours: float = Field(default=24.0, gt=0)


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
