from __future__ import annotations

import json
from typing import Any

import httpx2
from mcp import Client

from kubernetes_mcp.config import KubeCredentials, ServerSettings
from kubernetes_mcp.kube import KubeClient
from kubernetes_mcp.server import create_server
from tests.fake_api import LAST_APPLIED, OOM_POD, SECRET_VALUE, handler, kube

# A huge look-back so fixed fixture timestamps stay valid whenever the tests run.
SETTINGS = ServerSettings(
    allowed_namespaces=("prod",), max_results=3, max_log_lines=3, max_since_hours=1e6
)
READ_TOOLS = {
    "list_namespaces",
    "list_pods",
    "get_pod",
    "list_events",
    "list_deployments",
    "get_deployment",
    "get_pod_logs",
    "list_services",
}


async def call(
    tool: str, args: dict[str, Any], seen: list[httpx2.Request] | None = None, **kw: Any
) -> Any:
    seen = seen if seen is not None else []
    server = create_server(SETTINGS, kube(SETTINGS, seen, **kw))
    async with Client(server) as client:
        return await client.call_tool(tool, args)


def error_text(result: Any) -> str:
    assert result.is_error
    return str(result.content[0].text)


async def test_only_read_tools_exist() -> None:
    server = create_server(SETTINGS, kube(SETTINGS, []))
    async with Client(server) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
    assert tools == READ_TOOLS
    for word in ("delete", "exec", "scale", "patch", "apply", "restart", "create", "secret"):
        assert not any(word in t for t in tools)


async def test_list_pods_compacts_status_and_hides_secrets() -> None:
    seen: list[httpx2.Request] = []
    result = await call("list_pods", {"namespace": "prod"}, seen)
    data = result.structured_content
    assert seen[0].url.params["limit"] == "3"
    assert data["by_phase"] == {"Running": 1, "Pending": 1}
    oom = data["pods"][0]
    assert oom["name"] == "order-service-7d9c-abcde"
    assert (oom["ready"], oom["restarts"], oom["owner"]) == (
        "0/1",
        3,
        "ReplicaSet/order-service-7d9c",
    )
    app = oom["containers"][0]
    assert app["state"] == {
        "state": "waiting",
        "reason": "CrashLoopBackOff",
        "message": "back-off 40s",
    }
    assert app["last_state"]["reason"] == "OOMKilled" and app["last_state"]["exit_code"] == 137
    assert data["pods"][1]["containers"][0]["state"]["reason"] == "ImagePullBackOff"
    encoded = json.dumps(data)
    for leak in (SECRET_VALUE, "DATABASE_URL", "app-db", "order-service-config", LAST_APPLIED):
        assert leak not in encoded


async def test_get_pod_detail_without_env_or_exec_command() -> None:
    result = await call("get_pod", {"namespace": "prod", "name": OOM_POD["metadata"]["name"]})
    pod = result.structured_content["pod"]
    app = pod["containers"][0]
    assert app["resources"] == {"requests": {"memory": "64Mi"}, "limits": {"memory": "96Mi"}}
    assert app["readiness_probe"]["target"] == "http GET /ready port http"
    assert app["liveness_probe"]["target"] == "exec"
    assert pod["conditions"][0]["reason"] == "ContainersNotReady"
    assert SECRET_VALUE not in json.dumps(pod) and "env" not in app


async def test_not_found_is_a_clear_tool_error() -> None:
    result = await call("get_pod", {"namespace": "prod", "name": "nope"})
    assert "pod 'nope' in namespace 'prod' not found" in error_text(result)


async def test_guardrails_reject_without_api_calls() -> None:
    seen: list[httpx2.Request] = []
    bad: list[tuple[str, dict[str, Any], str]] = [
        ("list_pods", {"namespace": "kube-system"}, "not allowed"),
        ("get_pod", {"namespace": "default", "name": "x"}, "not allowed"),
        ("get_pod", {"namespace": "prod", "name": "../secrets/x"}, "invalid pod name"),
        (
            "list_pods",
            {"namespace": "prod", "label_selector": "app=$(rm -rf)"},
            "invalid label_selector",
        ),
        ("list_pods", {"namespace": "prod", "limit": 0}, "limit must be >= 1"),
        ("list_events", {"namespace": "prod", "since": "yesterday"}, "since must be"),
        ("list_events", {"namespace": "prod", "object_kind": "Pod,type=x"}, "invalid object_kind"),
        ("get_pod_logs", {"namespace": "prod", "name": "p", "tail_lines": 0}, "tail_lines"),
        ("get_pod_logs", {"namespace": "prod", "name": "p", "container": "a b"}, "container name"),
        ("get_deployment", {"namespace": "prod", "name": "x", "history": 0}, "history"),
    ]
    for tool, args, message in bad:
        assert message in error_text(await call(tool, args, seen)), (tool, args)
    assert seen == []


async def test_list_events_pages_sorts_and_filters() -> None:
    seen: list[httpx2.Request] = []
    result = await call("list_events", {"namespace": "prod", "limit": 10}, seen)
    data = result.structured_content
    assert [r.url.params.get("continue") for r in seen] == [None, "page2"]  # followed paging
    assert data["total"] == 5 and data["returned"] == 3 and data["truncated"]  # max_results=3
    times = [e["last_seen"] for e in data["events"]]
    assert times == sorted(times)  # oldest first ...
    assert times[0].startswith("2026-09-25T10:05")  # ... of the 3 most recent
    series = next(e for e in data["events"] if e["reason"] == "OOMKilling")
    assert series["count"] == 4 and series["last_seen"].startswith("2026-09-25T10:13")
    assert series["object"] == "Pod/order-service-7d9c-abcde" and series["source"] == "kubelet"

    result = await call(
        "list_events",
        {
            "namespace": "prod",
            "type": "Warning",
            "object_name_prefix": "order-service",
            "since": "2026-09-25T09:00:00Z",
            "limit": 1,
        },
        seen := [],
    )
    data = result.structured_content
    assert seen[0].url.params["fieldSelector"] == "type=Warning"
    assert data["total"] == 2  # BackOff + OOMKilling; the 08:00 Unhealthy is before `since`
    assert data["returned"] == 1 and data["truncated"]
    assert data["events"][0]["reason"] == "BackOff"  # the most recent one is kept

    await call(
        "list_events",
        {"namespace": "prod", "object_name": "user-service", "object_kind": "Deployment"},
        seen := [],
    )
    assert seen[0].url.params["fieldSelector"] == (
        "involvedObject.name=user-service,involvedObject.kind=Deployment"
    )


async def test_get_deployment_with_rollout_history() -> None:
    result = await call(
        "get_deployment", {"namespace": "prod", "name": "user-service", "history": 2}
    )
    data = result.structured_content
    dep = data["deployment"]
    assert dep["replicas"] == {
        "desired": 3,
        "updated": 1,
        "ready": 1,
        "available": 1,
        "unavailable": 2,
    }
    assert (dep["revision"], dep["change_cause"]) == ("3", "v3.2.0: bump image")
    assert dep["images"] == [{"container": "app", "image": "aiops/sample-service:v3.2.0"}]
    assert dep["restarted_at"] == "2026-09-25T10:02:00Z"
    assert data["revisions_total"] == 3  # the ReplicaSet of another deployment is excluded
    history = data["rollout_history"]
    assert [(h["revision"], h["current"]) for h in history] == [(3, True), (2, False)]
    assert history[1]["images"][0]["image"] == "aiops/sample-service:0.1.0"
    assert history[0]["change_cause"] == "v3.2.0: bump image"
    assert LAST_APPLIED not in json.dumps(data) and SECRET_VALUE not in json.dumps(data)


async def test_list_deployments() -> None:
    result = await call(
        "list_deployments", {"namespace": "prod", "label_selector": "app in (user-service,redis)"}
    )
    data = result.structured_content
    assert data["deployments"][0]["name"] == "user-service"
    assert data["deployments"][0]["conditions"][0]["reason"] == "MinimumReplicasUnavailable"


async def test_get_pod_logs_is_bounded() -> None:
    seen: list[httpx2.Request] = []
    result = await call(
        "get_pod_logs",
        {
            "namespace": "prod",
            "name": "order-service-7d9c-abcde",
            "tail_lines": 1000,
            "previous": True,
            "since": "15m",
        },
        seen,
    )
    params = seen[0].url.params
    assert params["tailLines"] == "3"  # capped by the server
    assert params["limitBytes"] == "64000" and params["previous"] == "true"
    assert params["sinceSeconds"] == "900"
    data = result.structured_content
    assert data["returned"] == 5 and data["lines"][0].endswith("line 0")
    missing = await call("get_pod_logs", {"namespace": "prod", "name": "missing"})
    assert "not found" in error_text(missing)


async def test_list_services_counts_ready_endpoints() -> None:
    result = await call("list_services", {"namespace": "prod"})
    services = {s["name"]: s for s in result.structured_content["services"]}
    assert services["user-service"]["endpoints"] == {"ready": 1, "not_ready": 1}
    assert services["redis"]["endpoints"] == {"ready": 0, "not_ready": 0}
    assert services["user-service"]["ports"][0]["node_port"] == 30083


async def test_list_namespaces_shows_the_allowlist() -> None:
    result = await call("list_namespaces", {})
    assert result.structured_content == {"namespaces": [{"name": "prod", "phase": "Active"}]}


async def test_rbac_denial_is_reported() -> None:
    result = await call("list_pods", {"namespace": "prod"}, status=403)
    assert "forbidden by RBAC" in error_text(result)


async def test_expired_token_is_reloaded_once() -> None:
    tokens = iter(["old-token", "new-token"])
    loads: list[str] = []

    def creds() -> KubeCredentials:
        loads.append(token := next(tokens))
        return KubeCredentials(server="https://k8s.test", token=token)

    seen: list[httpx2.Request] = []
    ok = handler(seen)

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.headers["Authorization"] == "Bearer old-token":
            return httpx2.Response(401, json={"message": "Unauthorized"})
        return ok(request)  # type: ignore[no-any-return]

    client = KubeClient(SETTINGS, credentials=creds, transport=httpx2.MockTransport(handle))
    server = create_server(SETTINGS, client)
    async with Client(server) as mcp:
        result = await mcp.call_tool("list_namespaces", {})
    assert not result.is_error and loads == ["old-token", "new-token"]
