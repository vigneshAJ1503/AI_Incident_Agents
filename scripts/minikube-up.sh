#!/usr/bin/env bash
# Start (or reuse) the lean local Kubernetes cluster and deploy the sample services.
# Budget: ~2.2 GB for the whole cluster (docs/setup/zero-cost.md).
set -euo pipefail

PROFILE=${MINIKUBE_PROFILE:-aiops}
K8S_VERSION=${K8S_VERSION:-v1.37.0}
MEMORY=${MINIKUBE_MEMORY:-2200}
CPUS=${MINIKUBE_CPUS:-2}
NETWORK=${MINIKUBE_NETWORK:-aiops}          # shared with docker compose: pods reach aiops-* containers by name
STATIC_IP=${MINIKUBE_STATIC_IP:-172.21.0.100}
IMAGE=aiops/sample-service:0.1.0
KUBECTL=(kubectl --context "$PROFILE")

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  echo "Docker network '$NETWORK' not found: run 'make infra-up' first." >&2
  exit 1
fi

if [[ "$(minikube status -p "$PROFILE" --format '{{.Host}}' 2>/dev/null || true)" != "Running" ]]; then
  echo "==> starting minikube profile '$PROFILE' (${MEMORY} MB, ${CPUS} CPUs, Kubernetes ${K8S_VERSION})"
  minikube start -p "$PROFILE" --driver=docker --kubernetes-version="$K8S_VERSION" \
    --memory="$MEMORY" --cpus="$CPUS" --disk-size=10g \
    --network="$NETWORK" --static-ip="$STATIC_IP" \
    --addons=default-storageclass,storage-provisioner \
    --wait=apiserver,system_pods,default_sa
fi

echo "==> building $IMAGE inside the cluster (no registry needed)"
minikube -p "$PROFILE" image build -t "$IMAGE" sample-services >/dev/null

echo "==> deploying sample services to namespace prod"
"${KUBECTL[@]}" apply -k deploy/k8s/base >/dev/null
# Pick up a rebuilt image with the same tag.
"${KUBECTL[@]}" -n prod rollout restart deployment -l 'app notin (postgres,redis)' >/dev/null
for deploy in postgres redis payment-service order-service user-service inventory-service traffic-generator; do
  if ! "${KUBECTL[@]}" -n prod rollout status "deployment/$deploy" --timeout=180s >/dev/null; then
    echo "!! $deploy did not become ready:" >&2
    "${KUBECTL[@]}" -n prod get pods -l "app=$deploy" >&2
    "${KUBECTL[@]}" -n prod get events --field-selector type=Warning --sort-by=.lastTimestamp | tail -5 >&2
    exit 1
  fi
done
echo "==> deploying kube-state-metrics (namespace monitoring, NodePort 30080)"
"${KUBECTL[@]}" apply -k deploy/k8s/monitoring >/dev/null
"${KUBECTL[@]}" -n monitoring rollout status deployment/kube-state-metrics --timeout=180s >/dev/null
"${KUBECTL[@]}" -n prod get pods -o wide
echo "==> NodePorts on ${STATIC_IP}: payment 30081, order 30082, user 30083, inventory 30084, kube-state-metrics 30080"
