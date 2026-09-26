"""MCP tools (read-only): list_namespaces, list_pods, get_pod, list_events,
list_deployments, get_deployment, get_pod_logs, list_services.

There is no tool that writes, execs, scales, restarts or reads Secrets/ConfigMaps, and
the HTTP client only issues GET requests. The ServiceAccount's RBAC (deploy/k8s/rbac)
is the second, independent layer.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from kubernetes_mcp.compact import (
    compact_deployment,
    compact_event,
    compact_pod,
    compact_replicaset,
    compact_service,
)
from kubernetes_mcp.config import ServerSettings
from kubernetes_mcp.guards import (
    GuardError,
    check_event_type,
    check_name,
    check_namespace,
    check_selector,
    clamp_limit,
    log_since_seconds,
    parse_since,
)
from kubernetes_mcp.kube import KubeClient, KubeError, KubeNotFoundError, Params

INSTRUCTIONS = """Read-only access to Kubernetes workloads in the allowed namespaces:
pods (status, restarts, last termination reason), deployments (replicas, rollout history
with images and change-cause), events, services and bounded pod logs. No writes, no exec,
no Secrets or ConfigMaps. Kubernetes keeps events for about 1 hour only."""

MAX_HISTORY = 10
SCAN_PAGE = 500

EventType = Literal["Warning", "Normal", "all"]


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def create_server(settings: ServerSettings, kube: KubeClient | None = None) -> MCPServer:
    client = kube or KubeClient(settings)
    server = MCPServer("kubernetes-mcp", instructions=INSTRUCTIONS, version="0.1.0")

    def guard(fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc

    async def api[T](coro: Awaitable[T], what: str | None = None) -> T:
        try:
            return await coro
        except KubeNotFoundError as exc:
            raise ToolError(f"{what or 'object'} not found") from exc
        except KubeError as exc:
            raise ToolError(str(exc)) from exc

    def ns(namespace: str) -> str:
        value: str = guard(check_namespace, namespace, settings.allowed_namespaces)
        return value

    def limited(limit: int) -> int:
        value: int = guard(clamp_limit, limit, settings.max_results)
        return value

    async def list_items(
        path: str, params: Params, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        data = await api(client.get_json(path, {**params, "limit": limit}))
        items: list[dict[str, Any]] = data.get("items") or []
        more = bool((data.get("metadata") or {}).get("continue"))
        return items[:limit], more or len(items) > limit

    async def scan_items(path: str, params: Params) -> tuple[list[dict[str, Any]], bool]:
        """All items, page by page, up to ``max_scan`` (for events and ReplicaSets, where
        the API order is not chronological and the newest item may be on a later page)."""
        items: list[dict[str, Any]] = []
        token: str | None = None
        while len(items) < settings.max_scan:
            page: Params = {**params, "limit": min(SCAN_PAGE, settings.max_scan - len(items))}
            if token:
                page["continue"] = token
            data = await api(client.get_json(path, page))
            items.extend(data.get("items") or [])
            token = (data.get("metadata") or {}).get("continue") or None
            if not token:
                return items, False
        return items[: settings.max_scan], True

    def selector_params(label_selector: str | None) -> Params:
        selector = guard(check_selector, label_selector)
        return {"labelSelector": selector} if selector else {}

    @server.tool()
    async def list_namespaces() -> dict[str, Any]:
        """The namespaces this server may read (the allowlist) and whether each exists."""
        namespaces = []
        for name in settings.allowed_namespaces:
            try:
                data = await client.get_json(f"/api/v1/namespaces/{name}")
                phase = (data.get("status") or {}).get("phase")
            except KubeNotFoundError:
                phase = "NotFound"
            except KubeError as exc:
                raise ToolError(str(exc)) from exc
            namespaces.append({"name": name, "phase": phase})
        return {"namespaces": namespaces}

    @server.tool()
    async def list_pods(
        namespace: str, label_selector: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Pods with phase, readiness, restarts, and each container's state and last
        termination (e.g. OOMKilled) or waiting reason (e.g. ImagePullBackOff, CrashLoopBackOff).

        Args:
            namespace: e.g. 'prod' (must be allowed by the server).
            label_selector: e.g. 'app=payment-service'.
            limit: max pods (capped by the server).
        """
        path = f"/api/v1/namespaces/{ns(namespace)}/pods"
        items, truncated = await list_items(path, selector_params(label_selector), limited(limit))
        pods = sorted((compact_pod(p) for p in items), key=lambda p: str(p["name"]))
        return {
            "namespace": namespace,
            "label_selector": label_selector,
            "returned": len(pods),
            "truncated": truncated,
            "by_phase": dict(Counter(str(p["phase"]) for p in pods)),
            "pods": pods,
        }

    @server.tool()
    async def get_pod(namespace: str, name: str) -> dict[str, Any]:
        """One pod in detail: containers (image, state, last state, resources, probes) and
        conditions. Environment variables and volumes are never returned."""
        pod_name = guard(check_name, name, "pod name")
        data = await api(
            client.get_json(f"/api/v1/namespaces/{ns(namespace)}/pods/{pod_name}"),
            f"pod '{pod_name}' in namespace '{namespace}'",
        )
        return {"pod": compact_pod(data, detail=True)}

    @server.tool()
    async def list_events(
        namespace: str,
        object_name: str | None = None,
        object_name_prefix: str | None = None,
        object_kind: str | None = None,
        type: EventType = "all",
        since: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Events (oldest first; the most recent `limit` are kept), e.g. BackOff, Unhealthy,
        Failed (image pull), OOMKilling, ScalingReplicaSet. Kubernetes keeps events ~1 hour.

        Args:
            namespace: e.g. 'prod'.
            object_name: exact involved object, e.g. a pod name.
            object_name_prefix: e.g. 'payment-service' matches the deployment, its
                ReplicaSets and pods.
            object_kind: e.g. 'Pod', 'Deployment', 'ReplicaSet'.
            type: 'Warning', 'Normal' or 'all'.
            since: only events last seen after this: '30m', '2h' or an ISO-8601 timestamp.
            limit: max events (capped by the server).
        """
        namespace = ns(namespace)
        guard(check_event_type, type)
        cap = limited(limit)
        since_ts = guard(parse_since, since, settings.max_since_hours)
        selectors = []
        if object_name:
            selectors.append(f"involvedObject.name={guard(check_name, object_name, 'object_name')}")
        if object_kind:
            if not object_kind.isalpha():
                raise ToolError(f"invalid object_kind '{object_kind}' (e.g. 'Pod')")
            selectors.append(f"involvedObject.kind={object_kind}")
        if type != "all":
            selectors.append(f"type={type}")
        prefix = (
            guard(check_name, object_name_prefix, "object_name_prefix")
            if object_name_prefix
            else None
        )
        params: Params = {"fieldSelector": ",".join(selectors)} if selectors else {}
        items, api_truncated = await scan_items(f"/api/v1/namespaces/{namespace}/events", params)
        events = [compact_event(e) for e in items]
        if prefix:
            events = [e for e in events if e["object"].split("/", 1)[-1].startswith(prefix)]
        if since_ts is not None:
            events = [e for e in events if (_ts(e["last_seen"]) or since_ts) >= since_ts]
        events.sort(key=lambda e: str(e["last_seen"] or ""))
        shown = events[-cap:]
        return {
            "namespace": namespace,
            "since": since_ts.isoformat() if since_ts else None,
            "total": len(events),
            "returned": len(shown),
            "truncated": api_truncated or len(events) > len(shown),
            "by_reason": dict(Counter(f"{e['type']}/{e['reason']}" for e in events)),
            "events": shown,
        }

    @server.tool()
    async def list_deployments(
        namespace: str, label_selector: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Deployments with desired/updated/ready/available replicas, images, revision,
        change-cause and conditions.

        Args:
            namespace: e.g. 'prod'.
            label_selector: e.g. 'app in (redis,postgres)'.
            limit: max deployments (capped by the server).
        """
        path = f"/apis/apps/v1/namespaces/{ns(namespace)}/deployments"
        items, truncated = await list_items(path, selector_params(label_selector), limited(limit))
        deployments = sorted((compact_deployment(d) for d in items), key=lambda d: str(d["name"]))
        return {
            "namespace": namespace,
            "returned": len(deployments),
            "truncated": truncated,
            "deployments": deployments,
        }

    @server.tool()
    async def get_deployment(namespace: str, name: str, history: int = 5) -> dict[str, Any]:
        """One deployment in detail plus its rollout history: its ReplicaSets by revision
        (newest first) with images, kubernetes.io/change-cause, creation (rollout) time and
        replicas; `current` marks the active revision.

        Args:
            namespace: e.g. 'prod'.
            name: deployment name, e.g. 'payment-service'.
            history: how many revisions to return (max 10).
        """
        namespace = ns(namespace)
        dep_name = guard(check_name, name, "deployment name")
        if history < 1:
            raise ToolError("history must be >= 1")
        data = await api(
            client.get_json(f"/apis/apps/v1/namespaces/{namespace}/deployments/{dep_name}"),
            f"deployment '{dep_name}' in namespace '{namespace}'",
        )
        deployment = compact_deployment(data, detail=True)
        match = deployment.get("selector") or {}
        selector = ",".join(f"{k}={v}" for k, v in sorted(match.items()))
        params: Params = {"labelSelector": selector} if selector else {}
        items, _ = await scan_items(f"/apis/apps/v1/namespaces/{namespace}/replicasets", params)
        owned = [
            rs
            for rs in items
            if any(
                ref.get("kind") == "Deployment" and ref.get("name") == dep_name
                for ref in (rs.get("metadata") or {}).get("ownerReferences") or []
            )
        ]
        revisions = sorted(
            (compact_replicaset(rs) for rs in owned),
            key=lambda r: r["revision"] or 0,
            reverse=True,
        )
        current = str(deployment.get("revision") or "")
        for rev in revisions:
            rev["current"] = str(rev["revision"]) == current
        return {
            "deployment": deployment,
            "rollout_history": revisions[: min(history, MAX_HISTORY)],
            "revisions_total": len(revisions),
        }

    @server.tool()
    async def get_pod_logs(
        namespace: str,
        name: str,
        container: str | None = None,
        tail_lines: int = 100,
        since: str | None = None,
        previous: bool = False,
    ) -> dict[str, Any]:
        """The last lines of a pod container's log (bounded by lines and bytes).

        Args:
            namespace: e.g. 'prod'.
            name: pod name (from list_pods).
            container: container name; needed only for multi-container pods.
            tail_lines: lines from the end (capped by the server).
            since: only lines newer than '15m', '1h' or an ISO-8601 timestamp.
            previous: the previous (crashed / OOMKilled) container instance's log.
        """
        namespace = ns(namespace)
        pod_name = guard(check_name, name, "pod name")
        if tail_lines < 1:
            raise ToolError("tail_lines must be >= 1")
        lines = min(tail_lines, settings.max_log_lines)
        params: Params = {
            "tailLines": lines,
            "limitBytes": settings.max_log_bytes,
            "timestamps": "true",
            "sinceSeconds": guard(log_since_seconds, since, settings.max_since_hours),
        }
        if container:
            params["container"] = guard(check_name, container, "container name")
        if previous:
            params["previous"] = "true"
        text = await api(
            client.get_text(f"/api/v1/namespaces/{namespace}/pods/{pod_name}/log", params),
            f"pod '{pod_name}' in namespace '{namespace}'",
        )
        out = text.splitlines()
        return {
            "pod": pod_name,
            "container": container,
            "previous": previous,
            "tail_lines": lines,
            "returned": len(out),
            "truncated_by_bytes": len(text.encode()) >= settings.max_log_bytes,
            "lines": out,
        }

    @server.tool()
    async def list_services(
        namespace: str, label_selector: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Services with type, selector, ports and how many endpoints are ready
        (0 ready = nothing can serve traffic).

        Args:
            namespace: e.g. 'prod'.
            label_selector: e.g. 'app=redis'.
            limit: max services (capped by the server).
        """
        namespace = ns(namespace)
        items, truncated = await list_items(
            f"/api/v1/namespaces/{namespace}/services",
            selector_params(label_selector),
            limited(limit),
        )
        slices, _ = await scan_items(
            f"/apis/discovery.k8s.io/v1/namespaces/{namespace}/endpointslices", {}
        )
        counts: dict[str, dict[str, int]] = {}
        for s in slices:
            owner = ((s.get("metadata") or {}).get("labels") or {}).get(
                "kubernetes.io/service-name"
            )
            if not owner:
                continue
            bucket = counts.setdefault(owner, {"ready": 0, "not_ready": 0})
            for endpoint in s.get("endpoints") or []:
                ready = (endpoint.get("conditions") or {}).get("ready", True)
                bucket["ready" if ready else "not_ready"] += 1
        services = sorted(
            (
                compact_service(
                    svc,
                    counts.get(
                        str((svc.get("metadata") or {}).get("name")), {"ready": 0, "not_ready": 0}
                    ),
                )
                for svc in items
            ),
            key=lambda s: str(s["name"]),
        )
        return {
            "namespace": namespace,
            "returned": len(services),
            "truncated": truncated,
            "services": services,
        }

    return server
