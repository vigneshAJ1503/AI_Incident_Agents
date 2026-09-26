"""Deterministic K8s analysis on hand-built kubernetes-mcp results (compact format)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aiops.agents.k8s_agent.analysis import K8sAnalysis, analyze
from aiops.core.models import TimeRange

START = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
WINDOW = TimeRange(start=START, end=START + timedelta(minutes=30))
INCIDENT = START + timedelta(minutes=10)


def ts(minutes: float) -> str:
    return (START + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def deployment(
    *,
    desired: int = 1,
    ready: int = 1,
    unavailable: int = 0,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "deployment": {
            "name": "order-service",
            "replicas": {
                "desired": desired,
                "updated": desired,
                "ready": ready,
                "available": ready,
                "unavailable": unavailable,
            },
            "revision": "4",
            "change_cause": "v2.3.0: enable in-memory order cache",
            "images": [{"container": "app", "image": "aiops/sample-service:0.1.0"}],
            "conditions": [
                {"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable"}
            ],
        },
        "rollout_history": history
        if history is not None
        else [revision(4, ts(-120), current=True), revision(3, ts(-600))],
    }


def revision(
    rev: int,
    created: str,
    *,
    image: str = "aiops/sample-service:0.1.0",
    current: bool = False,
    cause: str | None = None,
) -> dict[str, Any]:
    return {
        "revision": rev,
        "images": [{"container": "app", "image": image}],
        "change_cause": cause or f"release {rev}",
        "created_at": created,
        "current": current,
        "replicas": {"desired": 1 if current else 0, "ready": 1 if current else 0},
    }


def container(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "app",
        "image": "aiops/sample-service:0.1.0",
        "ready": True,
        "restart_count": 0,
        "state": {"state": "running", "started_at": ts(-100)},
        "last_state": None,
    }
    base.update(overrides)
    return base


def pods(
    *containers: dict[str, Any], names: list[str] | None = None, deleting: bool = False
) -> dict[str, Any]:
    names = names or [f"order-service-abc-{i}" for i in range(len(containers))]
    return {
        "pods": [
            {
                "name": name,
                "phase": "Running",
                "ready": "1/1" if c["ready"] else "0/1",
                "created_at": ts(-100),
                "deleting": deleting,
                "containers": [c],
            }
            for name, c in zip(names, containers, strict=True)
        ]
    }


def event(
    reason: str, obj: str, first: float, last: float, message: str = "", count: int = 1
) -> dict[str, Any]:
    return {
        "type": "Warning",
        "reason": reason,
        "object": obj,
        "message": message or reason,
        "count": count,
        "first_seen": ts(first),
        "last_seen": ts(last),
    }


def run(
    dep: Any = None,
    pod_data: Any = None,
    events: list[dict[str, Any]] | None = None,
    deps: Any = None,
    **kw: Any,
) -> K8sAnalysis:
    return analyze(
        dep if dep is not None else deployment(),
        pod_data if pod_data is not None else pods(container()),
        {"events": events or []},
        deps,
        service="order-service",
        deployment="order-service",
        namespace="prod",
        dependencies=kw.pop("dependencies", {}),
        window=WINDOW,
        incident_start=INCIDENT,
        incident_start_source="hint 'incident_start'",
        **kw,
    )


def test_healthy_workload() -> None:
    analysis = run()
    assert analysis.signals == ["healthy"]
    assert not analysis.problem
    assert "no restarts, no Warning events" in analysis.headline()
    assert "No rollout inside the window; latest revision 4" in "\n".join(analysis.lines())


def test_oom_kill_in_window_and_restarts() -> None:
    oom = container(
        ready=False,
        restart_count=3,
        state={"state": "waiting", "reason": "CrashLoopBackOff"},
        last_state={
            "state": "terminated",
            "reason": "OOMKilled",
            "exit_code": 137,
            "finished_at": ts(12),
        },
    )
    analysis = run(pod_data=pods(oom), dep=deployment(ready=0, unavailable=1))
    assert analysis.signals == ["pod_restarts", "oom_killed", "crash_loop", "replicas_unavailable"]
    assert "OOMKilled" in analysis.headline() and "CrashLoopBackOff" in analysis.headline()
    lines = "\n".join(analysis.lines())
    assert "last terminated OOMKilled (exit 137) at 2026-09-25T10:12:00Z" in lines


def test_old_restarts_before_the_window_are_ignored() -> None:
    old = container(
        restart_count=1,
        last_state={
            "state": "terminated",
            "reason": "OOMKilled",
            "exit_code": 137,
            "finished_at": ts(-90),
        },
    )
    assert run(pod_data=pods(old)).signals == ["healthy"]


def test_oom_from_events_and_backoff() -> None:
    events = [
        event("OOMKilling", "Pod/order-service-abc-0", 5, 12, "Memory cgroup out of memory"),
        event(
            "BackOff", "Pod/order-service-abc-0", 6, 13, "Back-off restarting failed container app"
        ),
    ]
    assert run(events=events).signals == ["oom_killed", "crash_loop"]


def test_image_pull_error_with_unavailable_replicas_and_rollout() -> None:
    history = [
        revision(
            13,
            ts(10),
            image="aiops/sample-service:v3.2.0",
            current=True,
            cause="v3.2.0: bump image",
        ),
        revision(12, ts(-5)),
    ]
    stuck = container(
        ready=False,
        image="aiops/sample-service:v3.2.0",
        state={"state": "waiting", "reason": "ImagePullBackOff"},
    )
    events = [
        event(
            "Failed",
            "Pod/user-service-new-1",
            10,
            14,
            'Failed to pull image "aiops/sample-service:v3.2.0"',
            5,
        )
    ]
    # the old ReplicaSet still serves 3/3; only unavailableReplicas shows the stuck rollout
    analysis = run(
        dep=deployment(desired=3, ready=3, unavailable=1, history=history),
        pod_data=pods(container(), stuck),
        events=events,
    )
    assert analysis.signals == ["image_pull_error", "replicas_unavailable", "recent_rollout"]
    rollout = analysis.rollouts[0]
    assert rollout.image_changed
    line = rollout.line(INCIDENT)
    assert "revision 13 rolled out 2026-09-25T10:10:00Z (+0s vs incident start)" in line
    assert "image aiops/sample-service:0.1.0 -> aiops/sample-service:v3.2.0" in line
    assert (
        "ImagePullBackOff" in analysis.headline()
        and "(1 unavailable, 3/3 updated)" in analysis.headline()
    )


def test_config_rollout_keeps_the_image() -> None:
    history = [
        revision(9, ts(8), current=True, cause="v1.8.2: tune db pool (DB_POOL_SIZE 20 -> 2)"),
        revision(8, ts(-20)),
    ]
    analysis = run(dep=deployment(history=history))
    assert analysis.signals == ["recent_rollout", "healthy"]
    assert analysis.problem  # a rollout is worth reporting
    assert "same image as revision 8" in analysis.rollouts[0].line(INCIDENT)
    assert (
        "rolled out revision 9 (v1.8.2: tune db pool (DB_POOL_SIZE 20 -> 2)) at 2026-09-25T10:08:00Z"
        in analysis.headline()
    )


def test_probe_failures_exclude_rollout_noise() -> None:
    live = pods(container(), names=["order-service-abc-0"])
    startup = event(
        "Unhealthy",
        "Pod/order-service-abc-0",
        1,
        1.2,
        "Readiness probe failed: connection refused",
        2,
    )
    replaced = event(
        "Unhealthy",
        "Pod/order-service-old-9",
        -3,
        5,
        "Readiness probe failed: connection refused",
        4,
    )
    assert run(pod_data=live, events=[startup, replaced]).signals == ["healthy"]
    lines = "\n".join(run(pod_data=live, events=[startup, replaced]).lines())
    assert "2 readiness failure(s) are rollout noise" in lines

    sustained = event(
        "Unhealthy", "Pod/order-service-abc-0", 1, 9, "Readiness probe failed: HTTP 503", 30
    )
    assert run(pod_data=live, events=[sustained]).signals == ["probe_failures"]
    liveness = event(
        "Unhealthy", "Pod/order-service-abc-0", 5, 5, "Liveness probe failed: timeout", 1
    )
    assert run(pod_data=live, events=[liveness]).signals == ["probe_failures"]
    terminating = pods(container(), names=["order-service-abc-0"], deleting=True)
    assert run(pod_data=terminating, events=[liveness]).signals == ["healthy"]


def test_dependencies_scaled_to_zero_missing_and_rolled_out() -> None:
    deps = {
        "deployments": [
            {
                "name": "redis",
                "replicas": {"desired": 0, "ready": 0},
                "revision": "1",
                "conditions": [],
            },
            {
                "name": "inventory-service",
                "replicas": {"desired": 1, "ready": 1},
                "revision": "7",
                "change_cause": "v1.4.2: stock query without index",
                "conditions": [
                    {
                        "type": "Progressing",
                        "status": "True",
                        "reason": "NewReplicaSetAvailable",
                        "last_update": ts(11),
                    }
                ],
            },
            {"name": "unrelated", "replicas": {"desired": 0, "ready": 0}},
        ]
    }
    analysis = run(
        deps=deps,
        dependencies={
            "redis": "redis",
            "inventory-service": "inventory-service",
            "postgres": "postgres",
        },
    )
    assert analysis.signals == ["dependency_unavailable", "dependency_rollout"]
    states = [d.state() for d in analysis.dependencies]
    assert states[0] == "redis: SCALED TO 0 replicas (0/0 ready): nothing can serve it"
    assert states[2].startswith("postgres: no deployment of that name")
    assert (
        "dependency inventory-service rolled out revision 7 (v1.4.2: stock query without index)"
        in analysis.headline()
    )


def test_missing_deployment_and_failed_queries_are_never_healthy() -> None:
    missing = run(dep={"error": "not found"}, pod_data={"pods": []})
    assert missing.signals == [] and not missing.found
    assert "was not found" in missing.headline()
    partial = run(failed=["events (timeout)"])
    assert "healthy" not in partial.signals
    assert "Could not query: events (timeout)." in partial.lines()


def test_events_before_the_window_are_dropped() -> None:
    old = event(
        "BackOff",
        "Pod/order-service-abc-0",
        -40,
        -35,
        "Back-off restarting failed container app",
        9,
    )
    assert run(events=[old]).signals == ["healthy"]
