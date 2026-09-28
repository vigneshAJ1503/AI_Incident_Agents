"""`aiops catalog import`: generate or update ``services.yaml`` from what a company has.

Sources (read-only):

* **kubernetes**: Deployments listed through the profile's ``k8s`` capability
  (kubernetes-mcp, allowlisted tools only). Label/annotation keys come from
  ``capabilities.k8s.settings.catalog_import`` (defaults below), never from code.
* **backstage**: ``catalog-info.yaml`` files (``kind: Component``) from a directory, or the
  Backstage REST API (``/api/catalog/entities``) with a token from an environment variable.

Merge semantics (default ``merge``): never overwrite a value that is already in the catalog;
only add new services, new identifiers and new list items (aliases, depends_on, ...).
Conflicting values are *kept* and reported. ``replace`` rewrites the file's own services.
The file is edited with ruamel.yaml, so comments and key order survive.
"""

from __future__ import annotations

import io
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx2
import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from aiops.core.catalog import CatalogFile, ServiceCatalog, merge_catalogs, normalize
from aiops.core.config import (
    CATALOG_FILE,
    ConfigError,
    Settings,
    extends_path,
    load_extending_yaml,
)
from aiops.mcp.toolset import Toolset

# --------------------------------------------------------------------------- options

#: Defaults for ``capabilities.k8s.settings.catalog_import`` (first match wins).
DEFAULT_K8S_IMPORT: dict[str, list[str]] = {
    "name_labels": ["app", "app.kubernetes.io/name"],
    "team_labels": ["team", "app.kubernetes.io/part-of", "owner"],
    "team_annotations": ["team", "owner"],  # key suffix: matches `example.com/team` too
}

#: Suffixes dropped to derive a short alias: payment-service -> payment.
ALIAS_SUFFIXES = ("-service", "-svc", "-api", "-app", "-server", "-srv", "-deployment")


@dataclass(frozen=True)
class MappingOptions:
    """Where imported identifiers go (label NAMES come from the profile's settings)."""

    environment: str | None = None  # catalog environment for per-namespace identifiers
    metrics_service_label: str | None = "service"  # None = metrics capability disabled
    metrics_namespace_label: str | None = "namespace"
    alerts_service_label: str | None = "service"
    logs: bool = True
    k8s_import: Mapping[str, list[str]] = field(default_factory=lambda: DEFAULT_K8S_IMPORT)

    @classmethod
    def from_settings(cls, settings: Settings, environment: str | None) -> MappingOptions:
        caps = settings.capabilities

        def label(cap: str, key: str) -> str | None:
            config = caps.get(cap)
            if config is None or not config.enabled:
                return None
            return str((config.settings.get("labels") or {}).get(key) or key)

        k8s = caps.get("k8s")
        custom = dict((k8s.settings.get("catalog_import") or {}) if k8s else {})
        return cls(
            environment=environment,
            metrics_service_label=label("metrics", "service"),
            metrics_namespace_label=label("metrics", "namespace"),
            alerts_service_label=label("alerts", "service"),
            logs=bool(caps.get("logs") and caps["logs"].enabled),
            k8s_import={**DEFAULT_K8S_IMPORT, **custom},
        )


def derive_aliases(name: str) -> list[str]:
    """'payment-service' -> ['payment']; names without a known suffix get none."""
    for suffix in ALIAS_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return [name[: -len(suffix)]]
    return []


def _service_identifiers(name: str, value: str, opts: MappingOptions) -> dict[str, Any]:
    caps: dict[str, Any] = {}
    if opts.logs:
        caps["logs"] = {"service_value": value}
    if opts.metrics_service_label:
        caps["metrics"] = {"labels": {opts.metrics_service_label: value}}
    if opts.alerts_service_label:
        caps["alerts"] = {"labels": {opts.alerts_service_label: value}}
    return caps


# --------------------------------------------------------------------------- kubernetes


def _first(mapping: Mapping[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        if mapping.get(key):
            return str(mapping[key])
    return None


def _annotation(annotations: Mapping[str, Any], suffixes: Iterable[str]) -> str | None:
    for suffix in suffixes:
        for key, value in sorted(annotations.items()):
            if (key == suffix or key.endswith("/" + suffix)) and value:
                return str(value)
    return None


def service_from_deployment(deployment: Mapping[str, Any], opts: MappingOptions) -> dict[str, Any]:
    """One compact kubernetes-mcp deployment (``get_deployment``) -> one catalog entry."""
    labels = deployment.get("labels") or {}
    dep_name = str(deployment["name"])
    namespace = deployment.get("namespace")
    rules = opts.k8s_import
    label_value = _first(labels, rules.get("name_labels", []))
    name = normalize(label_value or dep_name)
    team = _first(labels, rules.get("team_labels", [])) or _annotation(
        deployment.get("owner_annotations") or {}, rules.get("team_annotations", [])
    )
    selector = deployment.get("selector") or {}
    containers = [str(i.get("container")) for i in deployment.get("images") or []]
    k8s: dict[str, Any] = {"deployment": dep_name}
    if selector:
        k8s["label_selector"] = ",".join(f"{k}={v}" for k, v in sorted(selector.items()))
    if containers:
        k8s["container"] = dep_name if dep_name in containers else containers[0]

    entry: dict[str, Any] = {"name": name}
    aliases = derive_aliases(name)
    if aliases:
        entry["aliases"] = aliases
    if team:
        entry["owners"] = {"team": team}
    entry["capabilities"] = {**_service_identifiers(name, label_value or name, opts), "k8s": k8s}
    if namespace:
        env_caps: dict[str, Any] = {"k8s": {"namespace": namespace}}
        if opts.metrics_service_label and opts.metrics_namespace_label:
            env_caps["metrics"] = {
                "labels": {
                    opts.metrics_service_label: label_value or name,
                    opts.metrics_namespace_label: namespace,
                }
            }
        entry["environments"] = {opts.environment or str(namespace): {"capabilities": env_caps}}
    return entry


async def fetch_k8s_deployments(
    toolset: Toolset,
    namespaces: Iterable[str],
    *,
    label_selector: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """``list_deployments`` per namespace, then ``get_deployment`` for the selector (read-only)."""
    found: list[dict[str, Any]] = []
    for namespace in namespaces:
        args: dict[str, Any] = {"namespace": namespace, "limit": limit}
        if label_selector:
            args["label_selector"] = label_selector
        listed = await toolset.call("list_deployments", args)
        if not listed.ok:
            raise ConfigError(f"list_deployments in '{namespace}' failed: {listed.tool_call.error}")
        for item in (listed.data or {}).get("deployments") or []:
            detail = await toolset.call(
                "get_deployment", {"namespace": namespace, "name": item["name"], "history": 1}
            )
            if detail.ok and isinstance((detail.data or {}).get("deployment"), dict):
                found.append({**item, **detail.data["deployment"]})
            else:
                found.append(dict(item))
            found[-1].setdefault("namespace", namespace)
    return found


# --------------------------------------------------------------------------- backstage


def _entity_ref(ref: str) -> str:
    """'component:default/payment-service' -> 'payment-service'; 'group:team-a' -> 'team-a'."""
    ref = ref.split(":", 1)[-1]
    return ref.split("/", 1)[-1]


def service_from_backstage(entity: Mapping[str, Any], opts: MappingOptions) -> dict[str, Any]:
    """A Backstage ``kind: Component`` entity -> one catalog entry."""
    meta = entity.get("metadata") or {}
    spec = entity.get("spec") or {}
    ann: Mapping[str, Any] = meta.get("annotations") or {}
    name = normalize(str(meta["name"]))
    entry: dict[str, Any] = {"name": name}
    if meta.get("description"):
        entry["description"] = " ".join(str(meta["description"]).split())
    aliases = derive_aliases(name)
    title = normalize(str(meta.get("title") or ""))
    if title and title != name and title not in aliases:
        aliases.append(title)
    if aliases:
        entry["aliases"] = aliases
    if spec.get("owner"):
        entry["owners"] = {"team": _entity_ref(str(spec["owner"]))}
    depends = [_entity_ref(str(d)) for d in spec.get("dependsOn") or []]
    if depends:
        entry["depends_on"] = depends

    k8s_id = ann.get("backstage.io/kubernetes-id")
    caps = _service_identifiers(name, str(k8s_id or name), opts)
    if k8s_id or ann.get("backstage.io/kubernetes-label-selector"):
        k8s: dict[str, Any] = {}
        if k8s_id:
            k8s["deployment"] = str(k8s_id)
        k8s["label_selector"] = str(
            ann.get("backstage.io/kubernetes-label-selector")
            or f"backstage.io/kubernetes-id={k8s_id}"
        )
        if ann.get("backstage.io/kubernetes-namespace"):
            k8s["namespace"] = str(ann["backstage.io/kubernetes-namespace"])
        caps["k8s"] = k8s
    slug = ann.get("github.com/project-slug") or ann.get("gitlab.com/project-slug")
    if slug:
        caps["code"] = {"repo": str(slug).rsplit("/", 1)[-1]}
    tickets: dict[str, Any] = {}
    if ann.get("jira/project-key"):
        tickets["project_key"] = str(ann["jira/project-key"])
    if ann.get("jira/component"):
        tickets["components"] = [str(ann["jira/component"])]
    if tickets:
        caps["tickets"] = tickets
    if ann.get("pagerduty.com/service-id"):
        caps.setdefault("alerts", {})["pagerduty_service_id"] = str(ann["pagerduty.com/service-id"])
    entry["capabilities"] = caps
    return entry


def _components(docs: Iterable[Any]) -> list[dict[str, Any]]:
    return [
        d
        for d in docs
        if isinstance(d, dict)
        and str(d.get("kind", "")).lower() == "component"
        and isinstance(d.get("metadata"), dict)
        and d["metadata"].get("name")
    ]


def load_backstage_dir(path: Path) -> list[dict[str, Any]]:
    """Every ``kind: Component`` in ``catalog-info*.yaml`` files under ``path`` (multi-doc)."""
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted({*path.rglob("catalog-info*.yaml"), *path.rglob("catalog-info*.yml")})
    else:
        raise ConfigError(f"Backstage path not found: {path}")
    entities: list[dict[str, Any]] = []
    for file in files:
        try:
            entities.extend(_components(yaml.safe_load_all(file.read_text())))
        except yaml.YAMLError as exc:
            raise ConfigError(f"YAML syntax error in {file}: {exc}") from exc
    if not entities:
        raise ConfigError(f"No Backstage 'kind: Component' entities found under {path}")
    return entities


def fetch_backstage_api(
    url: str, token: str | None, *, transport: httpx2.BaseTransport | None = None
) -> list[dict[str, Any]]:
    """GET ``<url>/api/catalog/entities?filter=kind=component`` (read-only)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx2.Client(timeout=30.0, transport=transport, headers=headers) as client:
        response = client.get(
            url.rstrip("/") + "/api/catalog/entities", params={"filter": "kind=component"}
        )
    if response.status_code != 200:
        raise ConfigError(
            f"Backstage API {response.request.url} returned HTTP {response.status_code}"
        )
    data = response.json()
    items = data.get("items", []) if isinstance(data, dict) else data
    return _components(items)


def backstage_token(env_var: str) -> str | None:
    return os.environ.get(env_var) or None


# --------------------------------------------------------------------------- merge plan

_EMPTY: tuple[Any, ...] = (None, "", [], {})


@dataclass
class ServiceChange:
    name: str
    kind: Literal["new", "update", "unchanged"]
    added: dict[str, Any] = field(default_factory=dict)  # lists hold the FULL new list
    kept: list[tuple[str, Any, Any]] = field(default_factory=list)  # path, catalog, import


@dataclass
class ImportPlan:
    changes: list[ServiceChange]
    new_environments: list[str] = field(default_factory=list)
    mode: Literal["merge", "replace"] = "merge"
    imported: list[dict[str, Any]] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for c in self.changes if c.kind == kind)

    @property
    def conflicts(self) -> int:
        return sum(len(c.kept) for c in self.changes)

    @property
    def has_changes(self) -> bool:
        return (
            self.mode == "replace"
            or any(c.kind != "unchanged" for c in self.changes)
            or bool(self.new_environments)
        )


def _additions(
    existing: Mapping[str, Any], imported: Mapping[str, Any], path: str = ""
) -> tuple[dict[str, Any], list[tuple[str, Any, Any]]]:
    added: dict[str, Any] = {}
    kept: list[tuple[str, Any, Any]] = []
    for key, value in imported.items():
        where = f"{path}.{key}" if path else str(key)
        current = existing.get(key)
        if current in _EMPTY:
            if value not in _EMPTY:
                added[key] = value
        elif isinstance(value, dict) and isinstance(current, dict):
            sub_added, sub_kept = _additions(current, value, where)
            if sub_added:
                added[key] = sub_added
            kept.extend(sub_kept)
        elif isinstance(value, list) and isinstance(current, list):
            new_items = [v for v in value if v not in current]
            if new_items:
                added[key] = [*current, *new_items]
        elif current != value:
            kept.append((where, current, value))
    return added, kept


def plan_import(
    existing: Mapping[str, Any],
    imported: list[dict[str, Any]],
    *,
    mode: Literal["merge", "replace"] = "merge",
) -> ImportPlan:
    """Compare imported entries with the *resolved* catalog (``extends`` applied)."""
    services = {s["name"]: s for s in existing.get("services") or []}
    owner: dict[str, str] = {}
    for s in services.values():
        for alias in [s["name"], *(s.get("aliases") or [])]:
            owner.setdefault(normalize(str(alias)), s["name"])
    for s in imported:
        owner.setdefault(normalize(s["name"]), s["name"])

    changes: list[ServiceChange] = []
    known_envs = set((existing.get("environments") or {}).keys())
    new_envs: list[str] = []
    seen: set[str] = set()
    for entry in imported:
        name = entry["name"]
        if name in seen:
            continue  # the same service in two namespaces: first one wins
        seen.add(name)
        entry = dict(entry)
        # Aliases that another service already owns would make the catalog ambiguous.
        aliases = [a for a in entry.get("aliases") or [] if owner.get(normalize(a), name) == name]
        if aliases:
            entry["aliases"] = aliases
        else:
            entry.pop("aliases", None)
        for alias in aliases:
            owner.setdefault(normalize(alias), name)
        for env in entry.get("environments") or {}:
            if env not in known_envs and env not in new_envs:
                new_envs.append(env)
        if mode == "replace" or name not in services:
            body = {k: v for k, v in entry.items() if k != "name"}
            kind: Literal["new", "update", "unchanged"] = (
                "new" if name not in services else "update"
            )
            changes.append(ServiceChange(name, kind, body))
            continue
        added, kept = _additions(services[name], {k: v for k, v in entry.items() if k != "name"})
        changes.append(ServiceChange(name, "update" if added else "unchanged", added, kept))
    return ImportPlan(changes, new_envs, mode, imported)


# --------------------------------------------------------------------------- rendering


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict) and value:
        out: list[tuple[str, Any]] = []
        for k, v in value.items():
            out.extend(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return [(prefix, value)]


def render_plan(plan: ImportPlan) -> list[str]:
    """Human diff: ``+`` added, ``=`` kept (hand-edited value wins), ``·`` unchanged."""
    lines: list[str] = []
    for env in plan.new_environments:
        lines.append(f"+ environment {env}")
    for change in plan.changes:
        if change.kind == "new":
            lines.append(f"+ service {change.name} (new)")
        elif change.kind == "update" and plan.mode == "replace":
            lines.append(f"~ service {change.name} (replaced)")
        elif change.kind == "update":
            lines.append(f"~ service {change.name}")
        else:
            lines.append(f"· service {change.name} (unchanged)")
        for key, value in _flatten(change.added) if change.added else []:
            lines.append(f"    + {key}: {_short(value)}")
        for key, current, imported in change.kept:
            lines.append(f"    = {key}: kept {_short(current)} (import: {_short(imported)})")
    return lines


def _short(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(map(str, value)) + "]"
    return str(value)


# --------------------------------------------------------------------------- writing


def _yaml() -> YAML:
    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 4096  # never re-wrap long lines
    rt.indent(mapping=2, sequence=4, offset=2)
    return rt


def _deep_add(target: dict[str, Any], added: Mapping[str, Any]) -> None:
    for key, value in added.items():
        current = target.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            _deep_add(current, value)
        elif isinstance(value, list) and isinstance(current, list):
            for item in value:
                if item not in current:
                    current.append(item)
        else:
            target[key] = value


def apply_plan(target: Path, plan: ImportPlan, *, extends: str | None = None) -> str:
    """The new text of ``target`` (the profile's own services.yaml) with ``plan`` applied.

    ``extends``: parent catalog to reference when ``target`` doesn't exist yet.
    """
    rt = _yaml()
    if target.is_file():
        data = rt.load(target.read_text()) or CommentedMap()
    else:
        data = CommentedMap()
        if extends:
            data["extends"] = extends
    if plan.new_environments:
        envs = data.setdefault("environments", CommentedMap())
        for env in plan.new_environments:
            envs.setdefault(env, {"aliases": []})
    if plan.mode == "replace":
        data["services"] = CommentedSeq(
            [{"name": c.name, **c.added} for c in plan.changes if c.kind != "unchanged"]
        )
    else:
        services = data.setdefault("services", CommentedSeq())
        own = {str(s.get("name")): s for s in services if isinstance(s, dict)}
        for change in plan.changes:
            if change.kind == "unchanged":
                continue
            if change.name in own:
                _deep_add(own[change.name], change.added)
            else:
                services.append({"name": change.name, **change.added})
    out = io.StringIO()
    rt.dump(data, out)
    return out.getvalue()


def validate_catalog_text(target: Path, text: str) -> ServiceCatalog:
    """Resolve ``extends`` and validate the would-be catalog before writing it."""
    data = yaml.safe_load(text) or {}
    parent = data.pop("extends", None)
    if parent:
        base = load_extending_yaml(extends_path(target, str(parent)), merge_catalogs)
        data = merge_catalogs(base, data)
    return ServiceCatalog(CatalogFile.model_validate(data))


def resolved_catalog_data(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"services": []}
    return load_extending_yaml(path, merge_catalogs)


def target_catalog(settings: Settings) -> tuple[Path, str | None]:
    """Where to write: the profile's own services.yaml (created with ``extends`` if the
    catalog is inherited today)."""
    if settings.profile_dir is None:
        return settings.catalog_path(), None
    own = settings.profile_dir / CATALOG_FILE
    inherited = settings.service_catalog if settings.service_catalog != settings.profile else None
    return own, (None if own.is_file() else inherited)


_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


def check_names(imported: list[dict[str, Any]]) -> list[str]:
    """Names that don't look like catalog names (reported, still imported)."""
    return [s["name"] for s in imported if not _SAFE_NAME.match(s["name"])]
