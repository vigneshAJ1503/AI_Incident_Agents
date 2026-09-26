"""A fake Kubernetes API (httpx2 MockTransport) with the shapes the real API returns."""

from __future__ import annotations

from typing import Any

import httpx2

from kubernetes_mcp.config import KubeCredentials, ServerSettings
from kubernetes_mcp.kube import KubeClient

SECRET_VALUE = "-".join(["not", "a", "real", "password", "built", "at", "runtime"])
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def container(name: str = "app", image: str = "aiops/sample-service:0.1.0") -> dict[str, Any]:
    return {
        "name": name,
        "image": image,
        "env": [
            {"name": "DATABASE_PASSWORD", "value": SECRET_VALUE},
            {
                "name": "DATABASE_URL",
                "valueFrom": {"secretKeyRef": {"name": "app-db", "key": "DATABASE_URL"}},
            },
        ],
        "envFrom": [{"configMapRef": {"name": "order-service-config"}}],
        "resources": {"requests": {"memory": "64Mi"}, "limits": {"memory": "96Mi"}},
        "readinessProbe": {"httpGet": {"path": "/ready", "port": "http"}, "periodSeconds": 5},
        "livenessProbe": {"exec": {"command": ["sh", "-c", f"check --password {SECRET_VALUE}"]}},
    }


def pod(
    name: str, app: str, statuses: list[dict[str, Any]], phase: str = "Running"
) -> dict[str, Any]:
    rs = name.rsplit("-", 1)[0]
    return {
        "metadata": {
            "name": name,
            "namespace": "prod",
            "labels": {"app": app, "pod-template-hash": rs.rsplit("-", 1)[1]},
            "annotations": {LAST_APPLIED: '{"env": "' + SECRET_VALUE + '"}'},
            "creationTimestamp": "2026-09-25T10:00:00Z",
            "ownerReferences": [{"kind": "ReplicaSet", "name": rs, "controller": True}],
        },
        "spec": {"nodeName": "aiops", "containers": [container()]},
        "status": {
            "phase": phase,
            "startTime": "2026-09-25T10:00:01Z",
            "qosClass": "Burstable",
            "conditions": [{"type": "Ready", "status": "False", "reason": "ContainersNotReady"}],
            "containerStatuses": statuses,
        },
    }


OOM_POD = pod(
    "order-service-7d9c-abcde",
    "order-service",
    [
        {
            "name": "app",
            "ready": False,
            "restartCount": 3,
            "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 40s"}},
            "lastState": {
                "terminated": {
                    "reason": "OOMKilled",
                    "exitCode": 137,
                    "startedAt": "2026-09-25T10:10:00Z",
                    "finishedAt": "2026-09-25T10:12:00Z",
                }
            },
        }
    ],
)
PULL_POD = pod(
    "user-service-5f6b-fghij",
    "user-service",
    [
        {
            "name": "app",
            "ready": False,
            "restartCount": 0,
            "state": {"waiting": {"reason": "ImagePullBackOff", "message": "Back-off pulling"}},
        }
    ],
    phase="Pending",
)


def deployment(name: str, revision: str, cause: str, desired: int, ready: int) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": "prod",
            "generation": 4,
            "labels": {"app": name},
            "annotations": {
                "deployment.kubernetes.io/revision": revision,
                "kubernetes.io/change-cause": cause,
                LAST_APPLIED: "{}",
            },
            "creationTimestamp": "2026-09-24T08:00:00Z",
        },
        "spec": {
            "replicas": desired,
            "selector": {"matchLabels": {"app": name}},
            "strategy": {"type": "RollingUpdate"},
            "template": {
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/restartedAt": "2026-09-25T10:02:00Z"}
                },
                "spec": {"containers": [container(image="aiops/sample-service:v3.2.0")]},
            },
        },
        "status": {
            "observedGeneration": 4,
            "replicas": desired,
            "updatedReplicas": 1,
            "readyReplicas": ready,
            "availableReplicas": ready,
            "unavailableReplicas": desired - ready,
            "conditions": [
                {
                    "type": "Available",
                    "status": "False",
                    "reason": "MinimumReplicasUnavailable",
                    "message": "Deployment does not have minimum availability.",
                }
            ],
        },
    }


def replicaset(
    name: str, owner: str, revision: str, image: str, created: str, cause: str
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "labels": {"app": owner, "pod-template-hash": name.rsplit("-", 1)[1]},
            "annotations": {
                "deployment.kubernetes.io/revision": revision,
                "kubernetes.io/change-cause": cause,
            },
            "creationTimestamp": created,
            "ownerReferences": [{"kind": "Deployment", "name": owner, "controller": True}],
        },
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [{"name": "app", "image": image}]}},
        },
        "status": {"readyReplicas": 0, "availableReplicas": 0},
    }


DEPLOYMENTS = [deployment("user-service", "3", "v3.2.0: bump image", 3, 1)]
REPLICASETS = [
    replicaset(
        "user-service-5f6b",
        "user-service",
        "3",
        "aiops/sample-service:v3.2.0",
        "2026-09-25T10:02:00Z",
        "v3.2.0: bump image",
    ),
    replicaset(
        "user-service-1a2b",
        "user-service",
        "2",
        "aiops/sample-service:0.1.0",
        "2026-09-24T08:00:00Z",
        "v3.1.9",
    ),
    replicaset(
        "user-service-0c0d",
        "user-service",
        "1",
        "aiops/sample-service:0.0.9",
        "2026-09-20T08:00:00Z",
        "v3.1.8",
    ),
    replicaset(
        "user-service-other-9z9z",
        "user-service-other",
        "7",
        "x",
        "2026-09-25T10:00:00Z",
        "not owned",
    ),
]


def event(
    reason: str, obj: str, last: str, *, type_: str = "Warning", count: int = 1, kind: str = "Pod"
) -> dict[str, Any]:
    return {
        "metadata": {"name": f"{obj}.{reason}", "creationTimestamp": last},
        "type": type_,
        "reason": reason,
        "message": f"{reason} on {obj}",
        "involvedObject": {"kind": kind, "name": obj},
        "count": count,
        "firstTimestamp": last,
        "lastTimestamp": last,
        "source": {"component": "kubelet"},
    }


EVENTS_PAGE_1 = [
    event("BackOff", "order-service-7d9c-abcde", "2026-09-25T10:14:00Z", count=6),
    event("Failed", "user-service-5f6b-fghij", "2026-09-25T10:05:00Z", count=3),
]
EVENTS_PAGE_2 = [
    event(
        "ScalingReplicaSet",
        "user-service",
        "2026-09-25T10:02:00Z",
        type_="Normal",
        kind="Deployment",
    ),
    event("Unhealthy", "order-service-7d9c-abcde", "2026-09-25T08:00:00Z"),
    {  # new-style event: no firstTimestamp/lastTimestamp, a series instead
        "metadata": {"name": "series", "creationTimestamp": "2026-09-25T10:01:00Z"},
        "type": "Warning",
        "reason": "OOMKilling",
        "note": "Memory cgroup out of memory: Killed process 1234",
        "regarding": {"kind": "Pod", "name": "order-service-7d9c-abcde"},
        "eventTime": "2026-09-25T10:01:00.000000Z",
        "series": {"count": 4, "lastObservedTime": "2026-09-25T10:13:00.000000Z"},
        "reportingComponent": "kubelet",
    },
]

SERVICES = [
    {
        "metadata": {"name": "redis", "namespace": "prod"},
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": "redis"},
            "ports": [{"port": 6379, "targetPort": 6379}],
        },
    },
    {
        "metadata": {"name": "user-service", "namespace": "prod"},
        "spec": {
            "type": "NodePort",
            "selector": {"app": "user-service"},
            "ports": [{"name": "http", "port": 8080, "targetPort": "http", "nodePort": 30083}],
        },
    },
]
SLICES = [
    {
        "metadata": {"labels": {"kubernetes.io/service-name": "user-service"}},
        "endpoints": [{"conditions": {"ready": True}}, {"conditions": {"ready": False}}],
    }
]
LOG_TEXT = "\n".join(f"2026-09-25T10:00:0{i}Z line {i}" for i in range(5)) + "\n"


def handler(seen: list[httpx2.Request], *, status: int | None = None) -> Any:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        assert request.method == "GET"  # read-only by construction
        path, params = request.url.path, request.url.params
        if status is not None:
            return httpx2.Response(status, json={"message": "denied"})
        if path == "/api/v1/namespaces/prod":
            return httpx2.Response(200, json={"status": {"phase": "Active"}})
        if path.startswith("/api/v1/namespaces/") and path.count("/") == 4:
            return httpx2.Response(404, json={"message": "not found"})
        if path == "/api/v1/namespaces/prod/pods":
            items = [OOM_POD, PULL_POD]
            if params.get("labelSelector") == "app=order-service":
                items = [OOM_POD]
            return httpx2.Response(200, json={"items": items, "metadata": {}})
        if path == f"/api/v1/namespaces/prod/pods/{OOM_POD['metadata']['name']}":
            return httpx2.Response(200, json=OOM_POD)
        if path.endswith("/log"):
            if "missing" in path:
                return httpx2.Response(404, json={"message": "not found"})
            return httpx2.Response(200, text=LOG_TEXT)
        if path.startswith("/api/v1/namespaces/prod/pods/"):
            return httpx2.Response(404, json={"message": "pods not found"})
        if path == "/api/v1/namespaces/prod/events":
            if params.get("continue") == "page2":
                return httpx2.Response(200, json={"items": EVENTS_PAGE_2, "metadata": {}})
            return httpx2.Response(
                200, json={"items": EVENTS_PAGE_1, "metadata": {"continue": "page2"}}
            )
        if path == "/apis/apps/v1/namespaces/prod/deployments":
            return httpx2.Response(200, json={"items": DEPLOYMENTS, "metadata": {}})
        if path == "/apis/apps/v1/namespaces/prod/deployments/user-service":
            return httpx2.Response(200, json=DEPLOYMENTS[0])
        if path.startswith("/apis/apps/v1/namespaces/prod/deployments/"):
            return httpx2.Response(404, json={"message": "not found"})
        if path == "/apis/apps/v1/namespaces/prod/replicasets":
            return httpx2.Response(200, json={"items": REPLICASETS, "metadata": {}})
        if path == "/api/v1/namespaces/prod/services":
            return httpx2.Response(200, json={"items": SERVICES, "metadata": {}})
        if path == "/apis/discovery.k8s.io/v1/namespaces/prod/endpointslices":
            return httpx2.Response(200, json={"items": SLICES, "metadata": {}})
        return httpx2.Response(404, json={"message": f"unexpected {path}"})

    return handle


CREDS = KubeCredentials(server="https://k8s.test", token="test-token")


def kube(
    settings: ServerSettings, seen: list[httpx2.Request], *, status: int | None = None
) -> KubeClient:
    return KubeClient(
        settings,
        credentials=lambda: CREDS,
        transport=httpx2.MockTransport(handler(seen, status=status)),
    )
