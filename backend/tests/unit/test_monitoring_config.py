"""Consistency of the metrics stack config (PR-020): Prometheus scrape targets, the
kube-state-metrics flags the alert rules rely on, and the provisioned Grafana dashboards."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
PROM_DIR = ROOT / "deploy/compose/config/prometheus"
GRAFANA_DIR = ROOT / "deploy/compose/config/grafana"
K8S_BASE = ROOT / "deploy/k8s/base"
KSM = ROOT / "deploy/k8s/monitoring/kube-state-metrics.yaml"
NODE_IP = "172.21.0.100"


def docs(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def node_ports() -> dict[str, int]:
    """service name -> NodePort, from the sample-service manifests."""
    ports = {}
    for path in K8S_BASE.glob("*-service.yaml"):
        for doc in docs(path):
            if doc["kind"] == "Service" and doc["spec"].get("type") == "NodePort":
                ports[doc["metadata"]["name"]] = doc["spec"]["ports"][0]["nodePort"]
    return ports


def scrape_jobs() -> dict[str, dict[str, Any]]:
    config = yaml.safe_load((PROM_DIR / "prometheus.yml").read_text())
    return {job["job_name"]: job for job in config["scrape_configs"]}


def test_prometheus_scrapes_every_sample_service_with_its_labels() -> None:
    job = scrape_jobs()["sample-services"]
    assert job["honor_labels"] is True  # the apps' own service/team/namespace labels win
    targets = {c["targets"][0]: c["labels"] for c in job["static_configs"]}
    ports = node_ports()
    assert len(ports) == 4
    for service, port in ports.items():
        labels = targets[f"{NODE_IP}:{port}"]
        assert labels["service"] == service and labels["namespace"] == "prod"


def test_prometheus_loads_rules_and_sends_to_alertmanager() -> None:
    config = yaml.safe_load((PROM_DIR / "prometheus.yml").read_text())
    assert config["rule_files"] == ["/etc/prometheus/rules/alert-rules.yml"]
    targets = config["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert targets == ["aiops-alertmanager:9093"]
    assert config["global"]["scrape_interval"] == "30s"  # lean budget
    ksm = scrape_jobs()["kube-state-metrics"]
    assert ksm["static_configs"][0]["targets"] == [f"{NODE_IP}:30080"]


def test_kube_state_metrics_matches_the_alert_rules_contract() -> None:
    rules_header = (PROM_DIR / "alert-rules.yml").read_text()
    flag = "--metric-labels-allowlist=pods=[app,team],deployments=[app,team]"
    assert flag.removeprefix("--") in rules_header
    deployment = next(d for d in docs(KSM) if d["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert flag in container["args"]
    assert "--namespaces=prod" in container["args"]
    assert ":latest" not in container["image"] and "@" not in container["image"]
    assert container["securityContext"]["runAsNonRoot"] is True
    assert container["resources"]["limits"]["memory"]
    service = next(d for d in docs(KSM) if d["kind"] == "Service")
    assert service["spec"]["ports"][0]["nodePort"] == 30080
    role = next(d for d in docs(KSM) if d["kind"] == "ClusterRole")
    verbs = {v for rule in role["rules"] for v in rule["verbs"]}
    assert verbs == {"list", "watch"}  # read-only


def test_grafana_dashboards_are_provisioned_with_stable_uids() -> None:
    datasources = yaml.safe_load(
        (GRAFANA_DIR / "provisioning/datasources/datasources.yml").read_text()
    )["datasources"]
    assert {d["uid"] for d in datasources} == {"prometheus", "elasticsearch"}
    dashboards = {
        p.name: json.loads(p.read_text()) for p in (GRAFANA_DIR / "dashboards").glob("*.json")
    }
    assert {d["uid"] for d in dashboards.values()} == {
        "aiops-service-overview",
        "aiops-k8s-workloads",
    }
    for dashboard in dashboards.values():
        ids = [p["id"] for p in dashboard["panels"]]
        assert len(ids) == len(set(ids)), dashboard["uid"]  # panel deep links use viewPanel=<id>
        for panel in dashboard["panels"]:
            assert panel["datasource"]["uid"] == "prometheus"
            assert all(t["expr"] for t in panel["targets"])
