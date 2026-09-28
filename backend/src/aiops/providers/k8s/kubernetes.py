"""``k8s/kubernetes``: our read-only kubernetes-mcp (ADR-0008). Thin: no query language."""

from __future__ import annotations

from typing import ClassVar

from aiops.providers.base import Provider
from aiops.providers.registry import PROVIDER_REGISTRY


class KubernetesK8s(Provider):
    capability: ClassVar[str] = "k8s"
    name: ClassVar[str] = "kubernetes"
    mcp: ClassVar[str] = "mcp-servers/kubernetes-mcp"
    agent_tools: ClassVar[tuple[str, ...]] = ("get_deployment", "list_pods", "list_events")
    note: ClassVar[str] = "EKS/GKE/AKS/Minikube via a read-only ServiceAccount"


PROVIDER_REGISTRY.register(KubernetesK8s)
