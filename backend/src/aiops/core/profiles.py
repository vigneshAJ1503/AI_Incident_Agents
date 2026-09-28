"""Company profile tooling: provider matrix, validation, ``init`` and ``diff`` (ADR-0011).

``aiops profile validate`` turns a profile into a list of readable issues, e.g.
``capability logs: provider 'loki' needs setting 'labels.service'``. Loading
(``core.config``) already rejects malformed YAML; this adds the *semantic* checks that need
to know which providers exist and what they need.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from aiops.core.catalog import ServiceCatalog
from aiops.core.config import (
    CATALOG_FILE,
    PROFILE_FILE,
    ConfigError,
    Settings,
    load_yaml,
    referenced_env_vars,
)

Status = Literal["implemented", "planned"]


@dataclass(frozen=True)
class ProviderSpec:
    """What a provider of a capability needs. ``required`` = dotted ``settings`` keys."""

    status: Status
    mcp: str  # the MCP server that implements the capability's tool contract
    required: tuple[str, ...] = ()
    #: Tools the agent calls itself (must be in tool_allowlist, or the agent degrades).
    agent_tools: tuple[str, ...] = ()
    note: str = ""


#: Capability -> provider -> spec. "planned" = named in the roadmap (PR-P2..P4); a profile
#: may name it, but ``validate`` reports it as not implemented yet.
PROVIDERS: dict[str, dict[str, ProviderSpec]] = {
    "logs": {
        "elasticsearch": ProviderSpec(
            "implemented",
            "mcp-servers/elasticsearch-mcp",
            agent_tools=("execute_esql",),
            note="ES|QL; field names from settings.fields",
        ),
        "opensearch": ProviderSpec("planned", "opensearch-mcp", note="P4 candidate"),
        "loki": ProviderSpec(
            "planned", "loki-mcp", required=("labels.service",), note="LogQL; P4 candidate"
        ),
        "splunk": ProviderSpec("planned", "splunk-mcp", required=("index",), note="SPL"),
        "datadog": ProviderSpec("planned", "datadog-mcp", required=("site",)),
    },
    "metrics": {
        "prometheus": ProviderSpec(
            "implemented",
            "mcp-servers/prometheus-mcp",
            agent_tools=("query", "query_range"),
            note="any Prometheus-compatible API (Thanos, Mimir, VictoriaMetrics)",
        ),
        "datadog": ProviderSpec("planned", "datadog-mcp", required=("site",)),
    },
    "alerts": {
        "alertmanager": ProviderSpec(
            "implemented",
            "mcp-servers/alertmanager-mcp",
            agent_tools=("list_alerts", "list_silences"),
        ),
        "pagerduty": ProviderSpec("planned", "pagerduty-mcp", required=("service_ids",)),
        "opsgenie": ProviderSpec("planned", "opsgenie-mcp", required=("team",)),
    },
    "k8s": {
        "kubernetes": ProviderSpec(
            "implemented",
            "mcp-servers/kubernetes-mcp",
            agent_tools=("get_deployment", "list_pods", "list_events"),
            note="EKS/GKE/AKS/Minikube via a read-only ServiceAccount",
        ),
    },
    "code": {
        "git": ProviderSpec(
            "implemented",
            "mcp-servers/git-mcp",
            agent_tools=("list_releases", "search_commits", "get_diff"),
            note="local clones (mirror your GitHub/GitLab repos read-only)",
        ),
        "github": ProviderSpec("planned", "github-mcp", required=("owner",)),
        "gitlab": ProviderSpec("planned", "gitlab-mcp", required=("group",)),
    },
    "tickets": {
        "jira": ProviderSpec(
            "implemented",
            "sooperset/mcp-atlassian",
            required=("project_key",),
            agent_tools=("jira_search",),
            note="mcp-atlassian tool contract (Jira Cloud / Data Center)",
        ),
        "mock": ProviderSpec(
            "implemented",
            "mcp-servers/mock-tickets-mcp",
            required=("project_key",),
            agent_tools=("jira_search",),
            note="offline, same contract as jira",
        ),
    },
    "knowledge": {
        "postgres_fts": ProviderSpec(
            "implemented",
            "mcp-servers/knowledge-mcp",
            agent_tools=("search", "get_doc", "list_docs"),
            note="markdown runbooks -> Postgres full-text search",
        ),
        "confluence": ProviderSpec("planned", "mcp-atlassian", required=("spaces",)),
    },
}

#: LLM providers: implemented = accepted by ``llm.provider`` today.
LLM_PROVIDERS: dict[str, tuple[Status, str]] = {
    "openai_compat": ("implemented", "Groq, Gemini, OpenAI, Azure OpenAI, vLLM gateways, ..."),
    "fake": ("implemented", "tests only (zero tokens)"),
    "anthropic": ("planned", "Claude API"),
    "bedrock": ("planned", "AWS Bedrock"),
}


@dataclass(frozen=True)
class Issue:
    level: Literal["error", "warning"]
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.message}"


@dataclass
class ValidationReport:
    profile: str
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, message: str) -> None:
        self.issues.append(Issue("error", message))

    def warn(self, message: str) -> None:
        self.issues.append(Issue("warning", message))


def dotted_get(data: dict[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def check_settings(settings: Settings, catalog: ServiceCatalog | None = None) -> ValidationReport:
    """Semantic checks of a loaded profile (providers, required settings, tools, catalog)."""
    report = ValidationReport(settings.profile)
    for name, cap in sorted(settings.capabilities.items()):
        if not cap.enabled:
            continue
        where = f"capability {name}"
        providers = PROVIDERS.get(name)
        if providers is None:
            report.warn(
                f"{where}: no agent uses this capability yet (known: {', '.join(PROVIDERS)})"
            )
            continue
        spec = providers.get(cap.provider)
        implemented = sorted(p for p, s in providers.items() if s.status == "implemented")
        if spec is None:
            report.error(
                f"{where}: unknown provider '{cap.provider}' "
                f"(implemented: {', '.join(implemented)}; planned: "
                f"{', '.join(sorted(p for p, s in providers.items() if s.status == 'planned'))})"
            )
            continue
        for key in spec.required:
            if dotted_get(cap.settings, key) in (None, "", [], {}):
                report.error(
                    f"{where}: provider '{cap.provider}' needs setting '{key}' "
                    f"(capabilities.{name}.settings.{key})"
                )
        if spec.status == "planned":
            report.error(
                f"{where}: provider '{cap.provider}' is planned, not implemented yet "
                f"(roadmap PR-P2..P4; implemented: {', '.join(implemented)})"
            )
            continue
        missing_tools = [t for t in spec.agent_tools if t not in cap.tool_allowlist]
        if missing_tools:
            report.warn(
                f"{where}: tool_allowlist lacks {missing_tools}, which the {name} agent calls "
                "(its results will be partial)"
            )
    _check_llm(settings, report)
    if catalog is not None:
        _check_catalog(settings, catalog, report)
    return report


def _check_llm(settings: Settings, report: ValidationReport) -> None:
    llm = settings.llm
    if llm.provider == "openai_compat":
        if not llm.base_url:
            report.error("llm: provider 'openai_compat' needs 'base_url'")
        if llm.api_key is None:
            report.warn("llm: api_key is not set; live LLM calls will fail (replay evals work)")
        if not llm.models.get("agent"):
            report.warn("llm: models.agent is not set (set LLM_MODEL_AGENT)")


def _check_catalog(settings: Settings, catalog: ServiceCatalog, report: ValidationReport) -> None:
    if not catalog.services:
        report.warn("services.yaml: the service catalog is empty; agents can't resolve services")
        return
    logs = settings.capabilities.get("logs")
    shared_index = bool(logs and logs.settings.get("index_pattern"))
    envs = [None, *catalog.environments]
    for service in catalog.services:
        if logs and logs.enabled and not shared_index:
            has_index = any(service.identifiers("logs", env).get("index_pattern") for env in envs)
            if not has_index:
                report.warn(
                    f"service {service.name}: no logs.index_pattern in the catalog (nor "
                    "capabilities.logs.settings.index_pattern); the Log agent will skip it"
                )
        tickets = settings.capabilities.get("tickets")
        if tickets and tickets.enabled:
            ids = service.identifiers("tickets")
            if not (ids.get("components") or ids.get("labels")):
                report.warn(
                    f"service {service.name}: no tickets.components/labels; the Tickets agent "
                    "falls back to keyword search only"
                )


# --------------------------------------------------------------------------- env vars


def env_example_names(path: Path) -> set[str]:
    """Variable names in a ``.env.example`` (``NAME=...`` or commented ``# NAME=...``)."""
    names: set[str] = set()
    if not path.is_file():
        return names
    pattern = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")
    for line in path.read_text().splitlines():
        match = pattern.match(line)
        if match:
            names.add(match.group(1))
    return names


def profile_env_vars(profile_dir: Path) -> dict[str, bool]:
    """``{VAR: required}`` referenced by a profile's own profile.yaml and services.yaml."""
    found: dict[str, bool] = {}
    for file in (PROFILE_FILE, CATALOG_FILE):
        if (profile_dir / file).is_file():
            for name, required in referenced_env_vars(load_yaml(profile_dir / file)).items():
                found[name] = found.get(name, False) or required
    return found


def check_env_example(chain: tuple[Path, ...], report: ValidationReport) -> None:
    """Every ${VAR} a profile references is documented in its (or a parent's) .env.example."""
    documented: set[str] = set()
    for folder in chain:
        documented |= env_example_names(folder / ".env.example")
    if not chain:
        return
    undocumented = sorted(set(profile_env_vars(chain[0])) - documented)
    if undocumented:
        report.warn(
            f"{chain[0].name}/.env.example does not list {', '.join(undocumented)} "
            "(list every ${VAR} the profile uses)"
        )


# --------------------------------------------------------------------------- init

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def init_profile(
    profiles_dir: Path,
    name: str,
    source: str = "_template",
    *,
    company: str | None = None,
    description: str | None = None,
) -> Path:
    """Copy ``source`` to ``profiles/<name>`` and set its metadata. Never copies ``.env``."""
    if not _NAME.match(name):
        raise ConfigError(
            f"Invalid profile name '{name}': use lowercase letters, digits and '-' "
            "(e.g. 'acme' or 'acme-staging')."
        )
    src = profiles_dir / source
    if not (src / PROFILE_FILE).is_file():
        raise ConfigError(f"Source profile '{source}' not found in {profiles_dir}.")
    target = profiles_dir / name
    if target.exists():
        raise ConfigError(f"Profile '{name}' already exists: {target}")
    shutil.copytree(src, target, ignore=shutil.ignore_patterns(".env", "*.local.*", "__pycache__"))
    if source == "_template":
        # The template's README is about the template; the company gets docs/portability.md.
        (target / "README.md").unlink(missing_ok=True)
    profile = target / PROFILE_FILE
    text = profile.read_text()
    text = _set_metadata(text, "company", company or name)
    text = _set_metadata(text, "description", description or f"Profile for {company or name}")
    profile.write_text(text)
    return target


def _set_metadata(text: str, key: str, value: str) -> str:
    """Replace ``metadata.<key>`` in place (keeps comments), or add a metadata block."""
    quoted = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    block = re.search(r"^metadata:\s*\n((?:[ \t]+.*\n|\s*\n)*)", text, re.MULTILINE)
    if block is None:
        return f"metadata:\n  {key}: {quoted}\n\n{text}"
    body = block.group(1)
    line = re.compile(rf"^([ \t]+){key}:[^\n]*\n(?:\1[ \t]+[^\n]*\n)*", re.MULTILINE)
    match = line.search(body)
    if match:
        new_body = (
            body[: match.start()] + f"{match.group(1)}{key}: {quoted}\n" + body[match.end() :]
        )
    else:
        new_body = f"  {key}: {quoted}\n" + body
    return text[: block.start(1)] + new_body + text[block.end(1) :]


# --------------------------------------------------------------------------- show / diff

_SECRET_KEY = re.compile(r"(key|token|secret|password|passwd|credential)", re.IGNORECASE)
MASK = "********"


def mask_secrets(value: Any, key: str = "") -> Any:
    """Mask values whose key looks secret (``api_key``, ``*_token``, ``password``, ...)."""
    if isinstance(value, dict):
        return {k: mask_secrets(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v, key) for v in value]
    if key and _SECRET_KEY.search(key) and value not in (None, "", MASK):
        # Keep unresolved ${VAR} references visible: they're names, not secrets.
        if isinstance(value, str) and value.startswith("${"):
            return value
        return MASK
    return value


def resolved_dump(settings: Settings) -> dict[str, Any]:
    dump: dict[str, Any] = mask_secrets(settings.safe_dump())
    return dump


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """{'a': {'b': 1}} -> {'a.b': 1}; lists are leaves."""
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: value}


_MISSING = object()


def diff_dumps(a: dict[str, Any], b: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """Dotted keys whose values differ: ``(key, a_value, b_value)``; absent = ``None``."""
    fa, fb = flatten(a), flatten(b)
    rows = []
    for key in sorted(set(fa) | set(fb)):
        va, vb = fa.get(key, _MISSING), fb.get(key, _MISSING)
        if va != vb:
            rows.append((key, None if va is _MISSING else va, None if vb is _MISSING else vb))
    return rows


def catalog_summary(catalog: ServiceCatalog) -> dict[str, Any]:
    """Services and their identifiers, for diffs (``services.<name>.<cap>.<env>``)."""
    out: dict[str, Any] = {}
    for service in catalog.services:
        caps = set(service.capabilities)
        for env in service.environments.values():
            caps |= set(env.capabilities)
        out[service.name] = {
            cap: {
                "default": service.identifiers(cap),
                **{env: service.identifiers(cap, env) for env in catalog.environments},
            }
            for cap in sorted(caps)
        }
    return {"services": out}
