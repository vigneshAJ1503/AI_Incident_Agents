"""The ``k8s`` capability. Kubernetes has one API everywhere (EKS, GKE, AKS, OpenShift,
Minikube), so the provider is thin: it describes the kubernetes-mcp tool contract the K8s
agent calls, for the registry and ``aiops profile validate``. Only the MCP endpoint and
credentials vary per company (configuration)."""
