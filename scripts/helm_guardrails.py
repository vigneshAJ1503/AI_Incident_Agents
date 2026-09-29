"""Security guardrails on the rendered Helm chart (PR-047), beyond schema validity.

Reads `helm template` output on stdin and fails (exit 1) when any workload is missing the
hardened security context, resource limits or probes, when the chart renders a Secret, or when
a secret-looking environment variable carries an inline value instead of a secretKeyRef.
Usage: helm template ... | uv run --no-project --with pyyaml==6.0.3 python scripts/helm_guardrails.py LABEL
"""

from __future__ import annotations

import re
import sys
from typing import Any

import yaml

WORKLOADS = {"Deployment", "StatefulSet", "Job", "Pod"}
SECRETISH = re.compile(r"(PASSWORD|TOKEN|SECRET|API_KEY|APIKEY|_KEYS?$|DATABASE_URL)", re.I)


def pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    if doc["kind"] == "Pod":
        return dict(doc["spec"])
    return dict(doc["spec"]["template"]["spec"])


def check(doc: dict[str, Any]) -> list[str]:
    kind, name = doc.get("kind"), doc.get("metadata", {}).get("name")
    where = f"{kind}/{name}"
    if kind == "Secret":
        return [f"{where}: the chart must never render a Secret (existingSecret refs only)"]
    if kind not in WORKLOADS:
        return []
    spec = pod_spec(doc)
    errors: list[str] = []
    psc = spec.get("securityContext", {})
    if psc.get("runAsNonRoot") is not True:
        errors.append(f"{where}: pod securityContext.runAsNonRoot must be true")
    if not isinstance(psc.get("runAsUser"), int) or psc["runAsUser"] == 0:
        errors.append(f"{where}: pod securityContext.runAsUser must be a non-zero number")
    if psc.get("seccompProfile", {}).get("type") != "RuntimeDefault":
        errors.append(f"{where}: pod seccompProfile must be RuntimeDefault")
    token = spec.get("automountServiceAccountToken")
    if token is not False and "k8s-reader" not in str(spec.get("serviceAccountName")):
        errors.append(f"{where}: only the k8s-reader pod may mount a ServiceAccount token")
    for container in spec.get("initContainers", []) + spec.get("containers", []):
        c = f"{where}[{container['name']}]"
        sc = container.get("securityContext", {})
        if sc.get("readOnlyRootFilesystem") is not True:
            errors.append(f"{c}: readOnlyRootFilesystem must be true")
        if sc.get("allowPrivilegeEscalation") is not False:
            errors.append(f"{c}: allowPrivilegeEscalation must be false")
        if sc.get("capabilities", {}).get("drop") != ["ALL"]:
            errors.append(f"{c}: capabilities.drop must be [ALL]")
        limits = container.get("resources", {}).get("limits", {})
        if not {"cpu", "memory"} <= set(limits):
            errors.append(f"{c}: resources.limits.cpu and .memory are required")
        if kind in {"Deployment", "StatefulSet"} and not (
            container.get("readinessProbe") and container.get("livenessProbe")
        ):
            errors.append(f"{c}: readiness and liveness probes are required")
        if ":latest" in container["image"] or ":" not in container["image"]:
            errors.append(f"{c}: pin the image tag ({container['image']})")
        for env in container.get("env", []):
            if SECRETISH.search(env["name"]) and env.get("value"):
                errors.append(f"{c}: {env['name']} has an inline value; use a secretKeyRef")
    return errors


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "stdin"
    docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
    errors = [e for d in docs for e in check(d)]
    workloads = sum(d.get("kind") in WORKLOADS for d in docs)
    for error in errors:
        print(f"  {label}: {error}", file=sys.stderr)
    status = "FAIL" if errors else "ok"
    print(f"{label:<45} {status}: {len(docs)} objects, {workloads} workloads checked")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
