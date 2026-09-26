"""Turn Kubernetes API objects into compact, investigation-oriented dicts.

What is deliberately left out (data minimization, no secrets):
  * environment variables and envFrom (values can hold credentials; secretKeyRef names too),
  * volumes, service account tokens, managedFields,
  * annotations other than a small allowlist (kubectl's last-applied-configuration
    annotation contains the whole manifest).
"""

from __future__ import annotations

from typing import Any

CHANGE_CAUSE = "kubernetes.io/change-cause"
REVISION = "deployment.kubernetes.io/revision"
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
ANNOTATION_ALLOWLIST = (CHANGE_CAUSE, REVISION, "deployment.kubernetes.io/desired-replicas")
MESSAGE_CHARS = 300


def _text(value: Any, limit: int = MESSAGE_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _meta(obj: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = obj.get("metadata") or {}
    return meta


def safe_annotations(obj: dict[str, Any]) -> dict[str, str]:
    annotations = _meta(obj).get("annotations") or {}
    return {k: _text(v) or "" for k, v in annotations.items() if k in ANNOTATION_ALLOWLIST}


def _state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """{"waiting": {...}} / {"running": {...}} / {"terminated": {...}} -> one flat dict."""
    if not state:
        return None
    if "waiting" in state:
        w = state["waiting"] or {}
        return {"state": "waiting", "reason": w.get("reason"), "message": _text(w.get("message"))}
    if "running" in state:
        return {"state": "running", "started_at": (state["running"] or {}).get("startedAt")}
    if "terminated" in state:
        t = state["terminated"] or {}
        return {
            "state": "terminated",
            "reason": t.get("reason"),
            "exit_code": t.get("exitCode"),
            "started_at": t.get("startedAt"),
            "finished_at": t.get("finishedAt"),
            "message": _text(t.get("message")),
        }
    return None


def _resources(container: dict[str, Any]) -> dict[str, Any]:
    resources = container.get("resources") or {}
    return {
        "requests": resources.get("requests") or {},
        "limits": resources.get("limits") or {},
    }


def _probe(probe: dict[str, Any] | None) -> dict[str, Any] | None:
    if not probe:
        return None
    if "httpGet" in probe:
        target = f"http GET {probe['httpGet'].get('path', '/')} port {probe['httpGet'].get('port')}"
    elif "tcpSocket" in probe:
        target = f"tcp port {probe['tcpSocket'].get('port')}"
    elif "exec" in probe:
        target = "exec"  # the command may contain credentials: not shown
    elif "grpc" in probe:
        target = f"grpc port {probe['grpc'].get('port')}"
    else:
        target = "unknown"
    return {
        "target": target,
        "period_s": probe.get("periodSeconds", 10),
        "failure_threshold": probe.get("failureThreshold", 3),
        "initial_delay_s": probe.get("initialDelaySeconds", 0),
    }


def _owner(obj: dict[str, Any]) -> str | None:
    for ref in _meta(obj).get("ownerReferences") or []:
        if ref.get("controller"):
            return f"{ref.get('kind')}/{ref.get('name')}"
    return None


def compact_pod(pod: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    meta, spec, status = _meta(pod), pod.get("spec") or {}, pod.get("status") or {}
    statuses = {c.get("name"): c for c in status.get("containerStatuses") or []}
    containers = []
    for c in spec.get("containers") or []:
        st = statuses.get(c.get("name"), {})
        item: dict[str, Any] = {
            "name": c.get("name"),
            "image": c.get("image"),
            "ready": bool(st.get("ready", False)),
            "restart_count": int(st.get("restartCount", 0)),
            "state": _state(st.get("state")),
            "last_state": _state(st.get("lastState")),
        }
        if detail:
            item["resources"] = _resources(c)
            item["readiness_probe"] = _probe(c.get("readinessProbe"))
            item["liveness_probe"] = _probe(c.get("livenessProbe"))
            item["image_id"] = st.get("imageID") or None
        containers.append(item)
    ready = sum(1 for c in containers if c["ready"])
    result: dict[str, Any] = {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "phase": status.get("phase"),
        "reason": status.get("reason"),  # e.g. Evicted
        "ready": f"{ready}/{len(containers)}",
        "restarts": sum(c["restart_count"] for c in containers),
        "created_at": meta.get("creationTimestamp"),
        "started_at": status.get("startTime"),
        "deleting": bool(meta.get("deletionTimestamp")),
        "node": spec.get("nodeName"),
        "owner": _owner(pod),
        "pod_template_hash": (meta.get("labels") or {}).get("pod-template-hash"),
        "labels": meta.get("labels") or {},
        "containers": containers,
    }
    if detail:
        result["conditions"] = [
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": _text(c.get("message")),
                "last_transition": c.get("lastTransitionTime"),
            }
            for c in status.get("conditions") or []
        ]
        result["qos_class"] = status.get("qosClass")
    return result


def _images(template_spec: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"container": str(c.get("name")), "image": str(c.get("image"))}
        for c in template_spec.get("containers") or []
    ]


def compact_deployment(dep: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    meta, spec, status = _meta(dep), dep.get("spec") or {}, dep.get("status") or {}
    template = spec.get("template") or {}
    template_spec = template.get("spec") or {}
    annotations = meta.get("annotations") or {}
    desired = spec.get("replicas", 1)
    result: dict[str, Any] = {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "labels": meta.get("labels") or {},
        "replicas": {
            "desired": desired,
            "updated": status.get("updatedReplicas", 0),
            "ready": status.get("readyReplicas", 0),
            "available": status.get("availableReplicas", 0),
            "unavailable": status.get("unavailableReplicas", 0),
        },
        "revision": annotations.get(REVISION),
        "change_cause": _text(annotations.get(CHANGE_CAUSE)),
        "images": _images(template_spec),
        "restarted_at": ((template.get("metadata") or {}).get("annotations") or {}).get(
            RESTARTED_AT
        ),
        "paused": bool(spec.get("paused", False)),
        "observed_generation_current": status.get("observedGeneration") == meta.get("generation"),
        "created_at": meta.get("creationTimestamp"),
        "conditions": [
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": _text(c.get("message")),
                "last_update": c.get("lastUpdateTime"),
                "last_transition": c.get("lastTransitionTime"),
            }
            for c in status.get("conditions") or []
        ],
    }
    if detail:
        result["selector"] = (spec.get("selector") or {}).get("matchLabels") or {}
        result["strategy"] = (spec.get("strategy") or {}).get("type")
        result["resources"] = {
            str(c.get("name")): _resources(c) for c in template_spec.get("containers") or []
        }
    return result


def compact_replicaset(rs: dict[str, Any]) -> dict[str, Any]:
    meta, spec, status = _meta(rs), rs.get("spec") or {}, rs.get("status") or {}
    annotations = meta.get("annotations") or {}
    template = spec.get("template") or {}
    revision = str(annotations.get(REVISION) or "")
    return {
        "name": meta.get("name"),
        "revision": int(revision) if revision.isdigit() else None,
        "pod_template_hash": (meta.get("labels") or {}).get("pod-template-hash"),
        "images": _images(template.get("spec") or {}),
        "change_cause": _text(annotations.get(CHANGE_CAUSE)),
        "created_at": meta.get("creationTimestamp"),
        "restarted_at": ((template.get("metadata") or {}).get("annotations") or {}).get(
            RESTARTED_AT
        ),
        "replicas": {
            "desired": spec.get("replicas", 0),
            "ready": status.get("readyReplicas", 0),
            "available": status.get("availableReplicas", 0),
        },
    }


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    """core/v1 Event, including the newer series/eventTime fields."""
    meta = _meta(event)
    obj = event.get("involvedObject") or event.get("regarding") or {}
    series = event.get("series") or {}
    first = event.get("firstTimestamp") or event.get("eventTime") or meta.get("creationTimestamp")
    last = series.get("lastObservedTime") or event.get("lastTimestamp") or event.get("eventTime")
    source = event.get("source") or {}
    return {
        "type": event.get("type"),
        "reason": event.get("reason"),
        "object": f"{obj.get('kind')}/{obj.get('name')}",
        "message": _text(event.get("message") or event.get("note")),
        "count": int(str(series.get("count") or event.get("count") or 1)),
        "first_seen": first,
        "last_seen": last or first,
        "source": event.get("reportingComponent") or source.get("component"),
    }


def compact_service(svc: dict[str, Any], endpoints: dict[str, int] | None) -> dict[str, Any]:
    meta, spec = _meta(svc), svc.get("spec") or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "type": spec.get("type"),
        "selector": spec.get("selector") or {},
        "ports": [
            {
                "name": p.get("name"),
                "port": p.get("port"),
                "target_port": p.get("targetPort"),
                "node_port": p.get("nodePort"),
                "protocol": p.get("protocol", "TCP"),
            }
            for p in spec.get("ports") or []
        ],
        "endpoints": endpoints,  # {"ready": n, "not_ready": m}; None = unknown
    }
