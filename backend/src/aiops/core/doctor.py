"""`aiops doctor`: is this profile ready for day 1? (PR-P3, docs/portability.md)

For every enabled capability of a profile it checks, in order:

1. **config**: ``aiops profile validate`` issues for the capability; required ``${VAR}``s.
2. **reachability**: the MCP server connects and lists its tools in time (latency).
3. **contract**: ``list_tools`` covers the allowlist; write-looking tools are flagged.
4. **smoke**: one tiny read-only call, and a few catalog identifiers really resolve
   (index pattern has documents, deployment exists, repo exists, ...).

plus one **LLM** ping. Every call goes through the guarded :class:`Toolset` (allowlist,
timeout, redaction, audit), so doctor can never call a tool an agent couldn't.
Values of variables and secrets are never printed, only their names.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any

from aiops.core.catalog import ServiceCatalog, ServiceEntry
from aiops.core.config import CapabilityConfig, ConfigError, LLMConfig, Settings
from aiops.core.guardrails.audit import AuditSink, MemoryAuditSink
from aiops.core.profiles import ValidationReport, check_settings, validate_profile
from aiops.llm._common import mask
from aiops.llm.base import ChatMessage, LLMProvider
from aiops.llm.factory import config_problems, create_provider
from aiops.mcp.client import MCPClient, MCPClientError, ServerTarget, target_from_config
from aiops.mcp.toolset import ToolOutcome, Toolset


class Status(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class Check:
    capability: str  # capability name, "profile" or "llm"
    check: str  # config | reachability | contract | smoke | catalog | llm
    status: Status
    detail: str
    hint: str = ""
    latency_ms: float | None = None


@dataclass
class DoctorReport:
    profile: str
    checks: list[Check] = field(default_factory=list)

    def add(self, *checks: Check) -> None:
        self.checks.extend(checks)

    def count(self, status: Status) -> int:
        return sum(1 for c in self.checks if c.status == status)

    def exit_code(self, *, strict: bool = False) -> int:
        """0 = all OK, 1 = warnings with ``strict``, 2 = at least one FAIL."""
        if self.count(Status.FAIL):
            return 2
        if strict and self.count(Status.WARN):
            return 1
        return 0

    def as_dict(self, *, strict: bool = False) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "exit_code": self.exit_code(strict=strict),
            "summary": {s.value: self.count(s) for s in Status},
            "checks": [
                {**asdict(c), "status": c.status.value}
                for c in self.checks  # dataclass -> JSON-able
            ],
        }


# --------------------------------------------------------------------------- options


@dataclass(frozen=True)
class DoctorOptions:
    capabilities: tuple[str, ...] = ()  # empty = every enabled capability
    environment: str | None = "production"  # catalog environment for identifiers
    sample: int = 3  # catalog services checked per capability (never all of them)
    services: tuple[str, ...] = ()  # explicit services to check instead of the sample
    timeout_s: float = 10.0  # per connect / per call
    skip_llm: bool = False


#: Tool names that look like they change something. Word-bounded on '_' / '-' / case, so
#: ``list_silences`` or ``execute_esql`` (read-only ES|QL) don't match but
#: ``jira_create_issue``, ``delete_pod``, ``exec_in_pod`` and ``patchDeployment`` do.
_WRITE_WORDS = (
    "create|update|delete|remove|patch|put|post|exec|apply|scale|restart|rollback|"
    "write|edit|set|add|insert|upsert|drop|kill|evict|drain|cordon|uncordon|assign|"
    "transition|merge|push|upload|silence|ack|acknowledge|close|trigger"
)
_WRITE_TOOL = re.compile(rf"(?:^|[_\-.])(?:{_WRITE_WORDS})(?:$|[_\-.])")


def _split_camel(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def looks_like_write_tool(name: str) -> bool:
    return bool(_WRITE_TOOL.search(_split_camel(name)))


# --------------------------------------------------------------------------- smoke tests


@dataclass
class SmokeContext:
    """What a smoke test gets: the guarded toolset and a small sample of catalog services."""

    capability: str
    config: CapabilityConfig
    toolset: Toolset
    services: list[ServiceEntry]
    environment: str | None
    now: datetime

    def ids(self, service: ServiceEntry) -> dict[str, Any]:
        return service.identifiers(self.capability, self.environment)

    async def call(self, tool: str, args: dict[str, Any]) -> ToolOutcome | Check:
        """Call ``tool`` if allowlisted, else a SKIP check explaining why."""
        if not self.toolset.is_allowed(tool):
            return Check(
                self.capability,
                "smoke",
                Status.SKIP,
                f"smoke tool '{tool}' is not in tool_allowlist",
                f"add '{tool}' to capabilities.{self.capability}.tool_allowlist to enable it",
            )
        return await self.toolset.call(tool, args)

    def result(self, outcome: ToolOutcome, what: str) -> Check:
        """A FAIL check for a failed call, or an OK one with its latency."""
        ms = outcome.tool_call.duration_ms
        if not outcome.ok:
            return Check(
                self.capability,
                "smoke",
                Status.FAIL,
                f"{what}: {(outcome.tool_call.error or 'error')[:300]}",
                "check the MCP server's credentials and server-side allowlists",
                ms,
            )
        return Check(self.capability, "smoke", Status.OK, what, latency_ms=ms)

    def catalog(self, status: Status, detail: str, hint: str = "") -> Check:
        return Check(self.capability, "catalog", status, detail, hint)


SmokeTest = Callable[[SmokeContext], Awaitable[list[Check]]]

_CATALOG_HINT = "fix the identifier in services.yaml (or re-run `aiops catalog import`)"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _smoke_elasticsearch(ctx: SmokeContext) -> list[Check]:
    outcome = await ctx.call("list_indices", {})
    if isinstance(outcome, Check):
        return [outcome]
    checks = [ctx.result(outcome, "list_indices")]
    if not outcome.ok:
        return checks
    indices = (outcome.data or {}).get("indices") or []
    checks[0].detail = f"list_indices: {len(indices)} readable indices"
    settings = ctx.config.settings
    shared = settings.get("index_pattern")
    service_field = (settings.get("fields") or {}).get("service", "service")
    for service in ctx.services:
        ids = ctx.ids(service)
        pattern = ids.get("index_pattern") or shared
        if not pattern:
            checks.append(
                ctx.catalog(
                    Status.WARN,
                    f"{service.name}: no logs.index_pattern",
                    "set logs.index_pattern per service (or settings.index_pattern)",
                )
            )
            continue
        matching = [i for i in indices if fnmatchcase(str(i.get("index", "")), pattern)]
        docs = sum(int(i.get("docs") or 0) for i in matching)
        if not matching:
            checks.append(
                ctx.catalog(
                    Status.FAIL,
                    f"{service.name}: index pattern '{pattern}' matches no readable index",
                    f"{_CATALOG_HINT}; or allow it on the server (LOGS_ALLOWED_INDEX_PATTERNS)",
                )
            )
            continue
        if docs == 0:
            checks.append(ctx.catalog(Status.WARN, f"{service.name}: '{pattern}' has no documents"))
            continue
        # One tiny count for the service over the last hour (size 1: the total is enough).
        args: dict[str, Any] = {
            "index": pattern,
            "start": _iso(ctx.now - timedelta(hours=1)),
            "end": _iso(ctx.now),
            "size": 1,
            "fields": [str((settings.get("fields") or {}).get("timestamp", "@timestamp"))],
        }
        value = ids.get("service_value")
        if value and (shared or settings.get("service_filter")):
            args["query"] = f'{service_field}:"{value}"'
        count = await ctx.call("search_logs", args)
        if isinstance(count, Check):
            checks.append(
                ctx.catalog(Status.OK, f"{service.name}: '{pattern}' has {docs} documents")
            )
            continue
        if not count.ok:
            checks.append(ctx.result(count, f"{service.name}: 1h count on '{pattern}'"))
            continue
        total = int((count.data or {}).get("total") or 0)
        where = f"'{pattern}'" + (f" where {args['query']}" if "query" in args else "")
        checks.append(
            ctx.catalog(
                Status.OK if total else Status.WARN,
                f"{service.name}: {where}: {docs} docs, {total} in the last hour",
                ""
                if total
                else "no recent logs: fine for seeded demo data; otherwise check the "
                "service filter / shipping",
            )
        )
    return checks


def _loki_selector(base: str, extra: str | None) -> str:
    """``{namespace="prod"}`` + ``app="x"`` -> ``{namespace="prod", app="x"}``."""
    inner = base.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1].strip()
    return "{" + ", ".join(m for m in (inner, extra) if m) + "}"


async def _smoke_loki(ctx: SmokeContext) -> list[Check]:
    outcome = await ctx.call("list_labels", {})
    if isinstance(outcome, Check):
        return [outcome]
    checks = [ctx.result(outcome, "list_labels")]
    if not outcome.ok:
        return checks
    labels = (outcome.data or {}).get("labels") or []
    checks[0].detail = f"list_labels: {len(labels)} stream labels in the last hour"
    if not labels:
        checks[0].status = Status.WARN
        checks[0].hint = "no streams in the last hour: is the log shipper sending to Loki?"
    settings = ctx.config.settings
    service_label = str((settings.get("fields") or {}).get("service", "app"))
    filtered = bool(settings.get("service_filter")) and service_label in (
        settings.get("stream_labels") or []
    )
    for service in ctx.services:
        ids = ctx.ids(service)
        base = ids.get("index_pattern") or settings.get("index_pattern")
        if not base:
            checks.append(
                ctx.catalog(
                    Status.WARN,
                    f"{service.name}: no logs.index_pattern (stream selector)",
                    "set logs.index_pattern per service, e.g. '{namespace=\"prod\"}'",
                )
            )
            continue
        value = str(ids.get("service_value") or service.name)
        matcher = f'{service_label}="{value}"' if filtered else None
        query = f"sum(count_over_time({_loki_selector(str(base), matcher)} [1h]))"
        count = await ctx.call("query", {"query": query})
        if isinstance(count, Check):
            break
        if not count.ok:
            checks.append(ctx.result(count, f"{service.name}: {query}"))
            continue
        found = (count.data or {}).get("series") or []
        n = int(found[0].get("value") or 0) if found else 0
        checks.append(
            ctx.catalog(
                Status.OK if n else Status.WARN,
                f"{service.name}: {query} = {n}",
                ""
                if n
                else "no lines in the last hour: check the stream selector / service label "
                "or the log shipper",
            )
        )
    return checks


async def _smoke_prometheus(ctx: SmokeContext) -> list[Check]:
    outcome = await ctx.call("query", {"query": "count(up)"})
    if isinstance(outcome, Check):
        return [outcome]
    check = ctx.result(outcome, "query count(up)")
    if not outcome.ok:
        return [check]
    series = (outcome.data or {}).get("series") or []
    targets = int(series[0]["value"]) if series else 0
    check.detail = f"query count(up): {targets} scrape targets"
    if not targets:
        check.status, check.hint = Status.WARN, "Prometheus scrapes nothing (no `up` series)"
    checks = [check]
    settings = ctx.config.settings
    metric = (settings.get("metrics") or {}).get("requests")
    if not metric:
        return checks
    for service in ctx.services:
        labels = ctx.ids(service).get("labels") or {}
        if not labels:
            continue
        selector = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        # Any sample in the last hour: a pod that is down right now is not a catalog miss.
        query = f"count(count_over_time({metric}{{{selector}}}[1h]))"
        result = await ctx.call("query", {"query": query})
        if isinstance(result, Check):
            break
        if not result.ok:
            checks.append(ctx.result(result, f"{service.name}: {query}"))
            continue
        found = (result.data or {}).get("series") or []
        n = int(found[0]["value"]) if found else 0
        checks.append(
            ctx.catalog(
                Status.OK if n else Status.FAIL,
                f"{service.name}: {query} = {n}",
                "" if n else f"{_CATALOG_HINT} (metrics.labels) or check settings.labels",
            )
        )
    return checks


async def _smoke_alertmanager(ctx: SmokeContext) -> list[Check]:
    outcome = await ctx.call("list_alerts", {"state": "all", "limit": 1})
    if isinstance(outcome, Check):
        return [outcome]
    check = ctx.result(outcome, "list_alerts")
    if outcome.ok:
        data = outcome.data or {}
        total = data.get("total", data.get("returned", 0))
        check.detail = f"list_alerts: {total} alerts known to Alertmanager"
    return [check]


async def _smoke_kubernetes(ctx: SmokeContext) -> list[Check]:
    default_ns = ctx.config.settings.get("default_namespace")
    checks: list[Check] = []
    namespaces: set[str] = set()
    for service in ctx.services:
        ids = ctx.ids(service)
        namespace = ids.get("namespace") or default_ns
        deployment = ids.get("deployment")
        if not namespace or not deployment:
            checks.append(
                ctx.catalog(
                    Status.WARN,
                    f"{service.name}: no k8s.deployment/namespace",
                    "set k8s: {deployment, namespace} (or settings.default_namespace)",
                )
            )
            continue
        if namespace not in namespaces:
            namespaces.add(namespace)
            listed = await ctx.call("list_deployments", {"namespace": namespace, "limit": 1})
            if isinstance(listed, Check):
                checks.append(listed)
            else:
                check = ctx.result(listed, f"list_deployments in '{namespace}'")
                if not listed.ok:
                    check.hint = (
                        "namespace unknown or not allowed: check K8S_ALLOWED_NAMESPACES and "
                        "the ServiceAccount's RBAC"
                    )
                    checks.append(check)
                    continue
                checks.append(check)
        found = await ctx.call(
            "get_deployment", {"namespace": namespace, "name": deployment, "history": 1}
        )
        if isinstance(found, Check):
            break
        if not found.ok:
            checks.append(
                ctx.catalog(
                    Status.FAIL,
                    f"{service.name}: deployment '{deployment}' not found in '{namespace}'",
                    _CATALOG_HINT,
                )
            )
            continue
        replicas = ((found.data or {}).get("deployment") or {}).get("replicas") or {}
        checks.append(
            ctx.catalog(
                Status.OK,
                f"{service.name}: deployment {namespace}/{deployment} "
                f"({replicas.get('ready', '?')}/{replicas.get('desired', '?')} ready)",
            )
        )
    return checks


async def _smoke_git(ctx: SmokeContext) -> list[Check]:
    outcome = await ctx.call("list_repositories", {})
    if isinstance(outcome, Check):
        return [outcome]
    check = ctx.result(outcome, "list_repositories")
    if not outcome.ok:
        return [check]
    repos = set((outcome.data or {}).get("repositories") or [])
    check.detail = f"list_repositories: {len(repos)} repositories"
    checks = [check]
    for service in ctx.services:
        repo = ctx.ids(service).get("repo")
        if not repo:
            checks.append(ctx.catalog(Status.WARN, f"{service.name}: no code.repo"))
        elif repo in repos:
            checks.append(ctx.catalog(Status.OK, f"{service.name}: repo '{repo}' exists"))
        else:
            checks.append(
                ctx.catalog(
                    Status.FAIL,
                    f"{service.name}: repo '{repo}' is not served by the code MCP server",
                    f"{_CATALOG_HINT}; or mirror the repo for git-mcp",
                )
            )
    return checks


async def _smoke_jira(ctx: SmokeContext) -> list[Check]:
    project = ctx.config.settings.get("project_key")
    jql = f"project = {project} ORDER BY updated DESC" if project else "ORDER BY updated DESC"
    outcome = await ctx.call("jira_search", {"jql": jql, "limit": 1, "fields": "summary"})
    if isinstance(outcome, Check):
        return [outcome]
    check = ctx.result(outcome, f"jira_search '{jql}' (max 1)")
    if not outcome.ok:
        check.hint = "check settings.project_key and the Jira user's project permissions"
    else:
        total = (outcome.data or {}).get("total", 0)
        check.detail = f"jira_search project {project}: {total} issues"
        if not total:
            check.status = Status.WARN
            check.hint = "the project has no issues: similar-incident search will be empty"
    return [check]


async def _smoke_postgres_fts(ctx: SmokeContext) -> list[Check]:
    outcome = await ctx.call("list_docs", {})
    if isinstance(outcome, Check):
        return [outcome]
    check = ctx.result(outcome, "list_docs")
    if not outcome.ok:
        return [check]
    docs = (outcome.data or {}).get("documents") or []
    total = (outcome.data or {}).get("total", len(docs))
    check.detail = f"list_docs: {total} documents"
    if not total:
        check.status, check.hint = Status.WARN, "no runbooks indexed (aiops knowledge ingest)"
    checks = [check]
    paths = [str(d.get("path", "")) for d in docs]
    for service in ctx.services:
        for runbook in service.runbooks[:2]:
            ok = any(p == runbook or p.endswith("/" + runbook) for p in paths)
            checks.append(
                ctx.catalog(
                    Status.OK if ok else Status.WARN,
                    f"{service.name}: runbook '{runbook}' "
                    + ("indexed" if ok else "not found in list_docs"),
                    "" if ok else "fix services.yaml runbooks or ingest the document",
                )
            )
    return checks


#: ``(capability, provider)`` -> smoke test. Keyed by provider because the smoke call is
#: part of the provider's tool contract (a new provider adds its own entry here).
SMOKE_TESTS: dict[tuple[str, str], SmokeTest] = {
    ("logs", "elasticsearch"): _smoke_elasticsearch,
    ("logs", "loki"): _smoke_loki,
    ("metrics", "prometheus"): _smoke_prometheus,
    ("alerts", "alertmanager"): _smoke_alertmanager,
    ("k8s", "kubernetes"): _smoke_kubernetes,
    ("code", "git"): _smoke_git,
    ("tickets", "jira"): _smoke_jira,
    ("tickets", "mock"): _smoke_jira,
    ("knowledge", "postgres_fts"): _smoke_postgres_fts,
}


# --------------------------------------------------------------------------- the doctor


def _config_checks(report: ValidationReport) -> tuple[list[Check], dict[str, list[Check]]]:
    """Split ``profile validate`` issues into profile-level and per-capability checks."""
    general: list[Check] = []
    per_cap: dict[str, list[Check]] = {}
    for issue in report.issues:
        status = Status.FAIL if issue.level == "error" else Status.WARN
        match = re.match(r"capability (\w+): (.*)", issue.message, re.DOTALL)
        if match:
            per_cap.setdefault(match.group(1), []).append(
                Check(match.group(1), "config", status, match.group(2), "edit profile.yaml")
            )
            continue
        if issue.message.startswith("llm:"):
            continue  # reported by the LLM check
        hint = "run `aiops profile validate` for details"
        if issue.message.startswith("required variable"):
            hint = "set it in profiles/<name>/.env, the repo .env or the shell"
        general.append(Check("profile", "config", status, issue.message, hint))
    return general, per_cap


def sample_services(
    catalog: ServiceCatalog | None, options: DoctorOptions
) -> tuple[list[ServiceEntry], list[str]]:
    """A few catalog services (never hundreds); unknown explicit names are returned too."""
    if catalog is None:
        return [], []
    if options.services:
        found, unknown = [], []
        for name in options.services:
            resolution = catalog.resolve(name)
            if resolution.service is None:
                unknown.append(name)
            else:
                found.append(resolution.service)
        return found, unknown
    return catalog.services[: max(options.sample, 0)], []


class Doctor:
    def __init__(
        self,
        settings: Settings,
        options: DoctorOptions | None = None,
        *,
        missing_vars: list[str] | None = None,
        overrides: Mapping[str, ServerTarget] | None = None,
        llm_factory: Callable[[LLMConfig], LLMProvider] | None = None,
        audit: AuditSink | None = None,
        validation: ValidationReport | None = None,
        now: datetime | None = None,
    ) -> None:
        """``overrides``: capability -> server target (in-process servers in tests).
        ``validation``: a precomputed ``profile validate`` report (default: computed)."""
        self.settings = settings
        self.options = options or DoctorOptions()
        self.missing_vars = missing_vars or []
        self._overrides = dict(overrides or {})
        self._llm_factory = llm_factory
        self._audit = audit or MemoryAuditSink()
        self._validation = validation
        self._now = now

    def selected_capabilities(self) -> list[str]:
        enabled = sorted(n for n, c in self.settings.capabilities.items() if c.enabled)
        if not self.options.capabilities:
            return enabled
        unknown = [c for c in self.options.capabilities if c not in enabled]
        if unknown:
            raise ConfigError(
                f"Capability {unknown} is not enabled in profile '{self.settings.profile}' "
                f"(enabled: {', '.join(enabled) or 'none'})."
            )
        return list(self.options.capabilities)

    async def run(self) -> DoctorReport:
        report = DoctorReport(self.settings.profile)
        validation = self._validation or validate_profile(self.settings.profile)
        general, per_cap = _config_checks(validation)
        for var in self.missing_vars:
            message = f"required variable {var} is not set"
            if not any(message in c.detail for c in general):
                general.append(
                    Check(
                        "profile",
                        "config",
                        Status.FAIL,
                        message,
                        "set it in profiles/<name>/.env, the repo .env or the shell",
                    )
                )
        if not general:
            general.append(Check("profile", "config", Status.OK, "profile validate: no issues"))
        report.add(*general)

        try:
            catalog: ServiceCatalog | None = ServiceCatalog.from_settings(self.settings)
        except ConfigError:
            catalog = None  # already reported by validate
        services, unknown = sample_services(catalog, self.options)
        for name in unknown:
            report.add(
                Check("profile", "catalog", Status.FAIL, f"service '{name}' is not in the catalog")
            )
        environment = (
            catalog.resolve_environment(self.options.environment) if catalog else None
        ) or None

        for name in self.selected_capabilities():
            cap_checks = per_cap.get(name) or [
                Check(name, "config", Status.OK, "provider and allowlist look right")
            ]
            report.add(*cap_checks)
            report.add(*await self._check_capability(name, services, environment))
        if self.options.skip_llm:
            report.add(Check("llm", "llm", Status.SKIP, "skipped (--skip-llm)"))
        else:
            report.add(await self._check_llm())
        return report

    # -- one capability ------------------------------------------------------------------

    def _client(self, name: str, config: CapabilityConfig) -> MCPClient:
        target = self._overrides.get(name) or target_from_config(config.mcp)
        timeout = min(self.options.timeout_s, config.mcp.timeout_s)
        headers = {} if name in self._overrides else config.mcp.header_values()
        return MCPClient(name, target, timeout_s=timeout, connect_attempts=1, headers=headers)

    async def _check_capability(
        self, name: str, services: list[ServiceEntry], environment: str | None
    ) -> list[Check]:
        config = self.settings.capability(name)
        where = config.mcp.url if config.mcp.transport == "http" else f"stdio:{config.mcp.command}"
        if name in self._overrides:
            where = "in-process"
        started = time.perf_counter()
        client = self._client(name, config)
        try:
            await client.connect()
        except MCPClientError as exc:
            return [
                Check(
                    name,
                    "reachability",
                    Status.FAIL,
                    f"cannot connect to {where}: {str(exc).split(': ', 1)[-1][:200]}",
                    "start the MCP server (make mcp-up) or fix capabilities."
                    f"{name}.mcp.url / the network path",
                )
            ]
        try:
            try:
                tools = await client.list_tools()
            except Exception as exc:  # any transport/protocol failure is a FAIL row
                return [
                    Check(
                        name,
                        "reachability",
                        Status.FAIL,
                        f"connected to {where} but list_tools failed: {type(exc).__name__}",
                        "check the server logs",
                    )
                ]
            latency = round((time.perf_counter() - started) * 1000, 1)
            checks = [
                Check(
                    name,
                    "reachability",
                    Status.OK,
                    f"{where}: {len(tools)} tools",
                    latency_ms=latency,
                ),
                *self._contract(name, config, {t.name for t in tools}),
            ]
            checks.extend(await self._smoke(name, config, client, services, environment))
            return checks
        finally:
            await client.close()

    def _contract(self, name: str, config: CapabilityConfig, exposed: set[str]) -> list[Check]:
        checks: list[Check] = []
        allowed = set(config.tool_allowlist)
        missing = sorted(allowed - exposed)
        if missing:
            checks.append(
                Check(
                    name,
                    "contract",
                    Status.FAIL,
                    f"allowlisted tools missing on the server: {', '.join(missing)}",
                    "wrong MCP server/version, or remove them from tool_allowlist",
                )
            )
        writes_allowed = sorted(t for t in allowed if looks_like_write_tool(t))
        if writes_allowed:
            checks.append(
                Check(
                    name,
                    "contract",
                    Status.FAIL,
                    f"tool_allowlist contains write-looking tools: {', '.join(writes_allowed)}",
                    "agents are read-only: move them to write_allowlist (approval-gated)",
                )
            )
        extra = sorted(exposed - allowed - set(config.write_allowlist))
        writes = [t for t in extra if looks_like_write_tool(t)]
        gated = sorted(set(config.write_allowlist) & exposed)
        if writes:
            checks.append(
                Check(
                    name,
                    "contract",
                    Status.WARN,
                    f"server exposes write tools: {', '.join(writes)}",
                    "keep them out of the allowlist and prefer a read-only credential",
                )
            )
        if not missing and not writes_allowed:
            detail = f"all {len(allowed)} allowlisted tools present"
            reads = [t for t in extra if t not in writes]
            if reads:
                detail += f"; not allowlisted: {', '.join(reads)}"
            if gated:
                detail += f"; approval-gated write tools: {', '.join(gated)}"
            checks.append(Check(name, "contract", Status.OK, detail))
        return checks

    async def _smoke(
        self,
        name: str,
        config: CapabilityConfig,
        client: MCPClient,
        services: list[ServiceEntry],
        environment: str | None,
    ) -> list[Check]:
        smoke = SMOKE_TESTS.get((name, config.provider))
        if smoke is None:
            return [
                Check(
                    name,
                    "smoke",
                    Status.SKIP,
                    f"no smoke test for provider '{config.provider}'",
                )
            ]
        timeout = min(self.options.timeout_s, config.limits.query_timeout_s)
        guarded = config.model_copy(
            update={"limits": config.limits.model_copy(update={"query_timeout_s": timeout})}
        )
        toolset = Toolset(
            name,
            guarded,
            client,
            agent="doctor",
            guardrails=self.settings.guardrails,
            audit=self._audit,
        )
        ctx = SmokeContext(
            capability=name,
            config=guarded,
            toolset=toolset,
            services=services,
            environment=environment,
            now=self._now or datetime.now(UTC),
        )
        try:
            return await smoke(ctx)
        except (MCPClientError, TimeoutError) as exc:
            return [Check(name, "smoke", Status.FAIL, f"smoke test failed: {exc}")]

    # -- LLM -----------------------------------------------------------------------------

    async def _check_llm(self) -> Check:
        llm = self.settings.llm
        if llm.provider == "fake":
            return Check(
                "llm",
                "llm",
                Status.WARN,
                "provider 'fake': no real LLM configured",
                "set LLM_PROVIDER=openai_compat and a hosted LLM key",
            )
        roles = " ".join(f"{role}={model}" for role, model in llm.models_by_role().items())
        label = f"{llm.provider} ({_llm_endpoint(llm)}) {roles}"
        problems = config_problems(llm)
        key_missing = [p for p in problems if p.startswith("llm.api_key")]
        if key_missing:
            return Check(
                "llm",
                "llm",
                Status.WARN,
                f"{label}: no LLM API key: agents will fail until an LLM key is set",
                f"set {llm.env_var('api_key') or 'the key'} (never commit it)",
            )
        if problems:
            return Check(
                "llm",
                "llm",
                Status.FAIL,
                f"{label}: " + "; ".join(problems),
                "see docs/setup/llm-providers.md",
            )
        if llm.provider == "bedrock" and self._llm_factory is None:
            from aiops.llm.bedrock import aws_credentials_found

            if not await asyncio.to_thread(aws_credentials_found):
                return Check(
                    "llm",
                    "llm",
                    Status.WARN,
                    f"{label}: no AWS credentials: agents will fail until the AWS chain "
                    "resolves credentials",
                    "aws sso login / AWS_PROFILE / IRSA / an instance role (never in YAML)",
                )
        config = llm.model_copy(
            update={"max_retries": 0, "timeout_s": min(llm.timeout_s, self.options.timeout_s)}
        )
        started = time.perf_counter()
        try:
            provider = (self._llm_factory or create_provider)(config)
            response = await provider.generate(
                [ChatMessage.user("Reply with one word: pong")], role="fast", max_tokens=1
            )
        except Exception as exc:  # any provider failure is a FAIL row
            message = mask(str(exc), llm.api_key)[:200]
            return Check(
                "llm",
                "llm",
                Status.FAIL,
                f"{label}: {type(exc).__name__}: {message}",
                "check the credentials, endpoint and model/deployment names",
            )
        ms = round((time.perf_counter() - started) * 1000, 1)
        return Check(
            "llm",
            "llm",
            Status.OK,
            f"{label}: one-token ping ok (model={response.model})",
            latency_ms=ms,
        )


def _llm_endpoint(llm: LLMConfig) -> str:
    """Where the LLM calls go, for the doctor row (never a secret)."""
    if llm.provider == "bedrock":
        from aiops.llm.bedrock import aws_region

        return llm.base_url or f"bedrock-runtime {aws_region() or 'no region'}"
    if llm.provider == "anthropic":
        return llm.base_url or "api.anthropic.com"
    if llm.provider == "azure_openai":
        return f"{llm.base_url} api-version={llm.api_version}"
    return llm.base_url or "-"


async def check_capability(
    settings: Settings,
    capability: str,
    *,
    timeout_s: float = 8.0,
    overrides: Mapping[str, ServerTarget] | None = None,
    audit: AuditSink | None = None,
) -> DoctorReport:
    """``aiops doctor -c <capability> --skip-llm`` on already-loaded ``settings`` (the Web
    UI's "Test connection", PR-046): config checks of that capability, reachability,
    contract, smoke and catalog rows; profile-level and LLM rows are left out. ``settings``
    may carry unsaved changes, so the config checks run on it rather than on the YAML."""
    try:
        catalog: ServiceCatalog | None = ServiceCatalog.from_settings(settings)
    except ConfigError:
        catalog = None
    doctor = Doctor(
        settings,
        DoctorOptions(capabilities=(capability,), timeout_s=timeout_s, skip_llm=True),
        overrides=overrides,
        audit=audit,
        validation=check_settings(settings, catalog),
    )
    report = await doctor.run()
    report.checks = [c for c in report.checks if c.capability == capability]
    return report
