"""Elasticsearch setup for REAL Kubernetes logs shipped by Fluent Bit (PR-016).

Fluent Bit (deploy/k8s/logging) writes daily indices ``logs-k8s-YYYY.MM.DD``. This
module installs, idempotently:

* an index template with the SAME app-field mappings as the synthetic logs (so the Log
  agent's queries behave identically), plus Kubernetes metadata under ``kubernetes.*``;
* an ILM policy deleting those indices after a short retention (lean disk and heap).

``logs-k8s-*`` also matches Elasticsearch's built-in ``logs-*-*`` data-stream template
(priority 100); ours has a higher priority so plain daily indices are created instead.
"""

from __future__ import annotations

import copy
from typing import Any

from aiops.seed.elasticsearch import ElasticsearchSeeder
from aiops.seed.logs import INDEX_TEMPLATE

K8S_INDEX_PREFIX = "logs-k8s"
K8S_TEMPLATE_NAME = "aiops-k8s-logs"
K8S_ILM_POLICY = "aiops-k8s-logs-retention"
DEFAULT_RETENTION = "2d"


def ilm_policy(retention: str = DEFAULT_RETENTION) -> dict[str, Any]:
    """Delete each daily index ``retention`` after it was created (no rollover)."""
    return {
        "policy": {
            "_meta": {"managed_by": "aiops", "description": "PR-016 real k8s log retention"},
            "phases": {
                "hot": {"min_age": "0ms", "actions": {}},
                "delete": {"min_age": retention, "actions": {"delete": {}}},
            },
        }
    }


def index_template(policy: str = K8S_ILM_POLICY) -> dict[str, Any]:
    app_fields = copy.deepcopy(INDEX_TEMPLATE["template"]["mappings"]["properties"])
    keyword = {"type": "keyword", "ignore_above": 256}
    properties: dict[str, Any] = {
        **app_fields,
        # Raw text of lines that aren't JSON (e.g. Postgres, Redis).
        "log": {"type": "text"},
        "kubernetes": {
            "properties": {
                "namespace_name": keyword,
                "pod_name": keyword,
                "container_name": keyword,
                "container_image": keyword,
                "host": keyword,
                "pod_id": keyword,
                # Labels vary per workload: map whatever arrives as keywords.
                "labels": {"type": "object", "dynamic": True},
            }
        },
    }
    return {
        "index_patterns": [f"{K8S_INDEX_PREFIX}-*"],
        "priority": 200,  # > the built-in logs-*-* data-stream template (100)
        "_meta": {"managed_by": "aiops", "description": "PR-016 real Kubernetes logs"},
        "template": {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "index.lifecycle.name": policy,
                # Fluent Bit bulk-writes every 2 s; a slower refresh saves heap and CPU.
                "index.refresh_interval": "5s",
            },
            "mappings": {
                "dynamic": "false",
                "dynamic_templates": [
                    {
                        "labels_as_keywords": {
                            "path_match": "kubernetes.labels.*",
                            "mapping": keyword,
                        }
                    }
                ],
                "properties": properties,
            },
        },
    }


def ensure_k8s_logging(url: str, *, retention: str = DEFAULT_RETENTION) -> str:
    """Install/refresh the ILM policy and index template (idempotent PUTs). Returns ES version."""
    seeder = ElasticsearchSeeder(url)
    try:
        version = seeder.ping()
        seeder.put(f"/_ilm/policy/{K8S_ILM_POLICY}", ilm_policy(retention), "create ILM policy")
        seeder.put(f"/_index_template/{K8S_TEMPLATE_NAME}", index_template(), "index template")
    finally:
        seeder.close()
    return version
