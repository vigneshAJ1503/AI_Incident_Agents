#!/usr/bin/env bash
# The Helm chart (PR-047, docs/setup/helm.md). Helm and kubeconform run in pinned containers:
# nothing to install but Docker (CI runs the same `lint`).
#   scripts/helm.sh lint                 make helm-lint              lint + render every values set + kubeconform
#   scripts/helm.sh template [values]    make helm-template          render (default values, or the given file)
#   scripts/helm.sh install-minikube     make helm-install-minikube  build + load the images, Secrets, helm upgrade --install
#   scripts/helm.sh uninstall-minikube   make helm-uninstall-minikube
# Env: HELM_RELEASE (aiops), HELM_NAMESPACE (aiops), MINIKUBE_PROFILE (aiops), SKIP_BUILD=1.
set -euo pipefail
cd "$(dirname "$0")/.."

HELM_IMAGE=alpine/helm:3.22.0
KUBECONFORM_IMAGE=ghcr.io/yannh/kubeconform:v0.8.0
# The newest Kubernetes schema kubeconform publishes; Minikube runs v1.37.
KUBE_VERSION=${KUBE_VERSION:-1.36.0}
CHART=ai-incident-agents                       # under deploy/helm/
RELEASE=${HELM_RELEASE:-aiops}
NAMESPACE=${HELM_NAMESPACE:-aiops}
PROFILE=${MINIKUBE_PROFILE:-aiops}
NETWORK=${MINIKUBE_NETWORK:-aiops}
KUBECONFIG_DIR=.data/helm

say() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }

helm_run() { # helm in a container; the chart dir is mounted read-only at /charts
  docker run --rm -i -v "$PWD/deploy/helm:/charts:ro" -w /charts "$@"
}

render() { # values file (relative to deploy/helm/), or "" for the chart defaults
  local args=(template "$RELEASE" "$CHART" --namespace "$NAMESPACE")
  [[ -n ${1:-} ]] && args+=(-f "$1")
  helm_run "$HELM_IMAGE" "${args[@]}"
}

lint() {
  # Every values set CI validates: the chart defaults ("") + ci/*.yaml + values-*.yaml.
  local sets=("") values
  mkdir -p .data/kubeconform-cache   # downloaded JSON schemas (git-ignored)
  while IFS= read -r values; do sets+=("$values"); done \
    < <(cd deploy/helm && ls "$CHART"/ci/*.yaml values-*.yaml)
  say "helm lint --strict ($HELM_IMAGE)"
  for values in "${sets[@]}"; do
    helm_run "$HELM_IMAGE" lint "$CHART" --strict --quiet ${values:+-f "$values"} </dev/null
  done
  say "helm template | kubeconform -strict (Kubernetes $KUBE_VERSION schemas, $KUBECONFORM_IMAGE)"
  for values in "${sets[@]}"; do
    printf '%-48s ' "${values:-(chart defaults)}"
    render "$values" </dev/null | docker run --rm -i -v "$PWD/.data/kubeconform-cache:/cache" \
      "$KUBECONFORM_IMAGE" -strict -summary -cache /cache \
      -kubernetes-version "$KUBE_VERSION" -output text -
  done
  say "Guardrails on the rendered manifests (scripts/helm_guardrails.py)"
  for values in "${sets[@]}"; do
    render "$values" </dev/null | uv run --quiet --no-project --with pyyaml==6.0.3 \
      python scripts/helm_guardrails.py "${values:-(chart defaults)}"
  done
}

kubeconfig() { # a kubeconfig the helm container can use: the node IP on the aiops network
  mkdir -p "$KUBECONFIG_DIR"
  local ip
  ip=$(minikube -p "$PROFILE" ip)
  kubectl config view --minify --flatten --context "$PROFILE" \
    | sed -E "s#server: https://[^ ]+#server: https://${ip}:8443#" > "$KUBECONFIG_DIR/kubeconfig"
  chmod 600 "$KUBECONFIG_DIR/kubeconfig"
}

helm_cluster() { # helm against Minikube, from a container on its Docker network
  docker run --rm -i --network "$NETWORK" \
    -v "$PWD/deploy/helm:/charts:ro" -v "$PWD/$KUBECONFIG_DIR/kubeconfig:/kube/config:ro" \
    -e KUBECONFIG=/kube/config -w /charts "$HELM_IMAGE" "$@"
}

IMAGES=(backend web mock-tickets-mcp)

install_minikube() {
  if [[ "$(minikube status -p "$PROFILE" --format '{{.Host}}' 2>/dev/null || true)" != "Running" ]]; then
    echo "Minikube profile '$PROFILE' is not running: minikube start -p $PROFILE (or make k8s-up)." >&2
    exit 1
  fi
  local kubectl=(kubectl --context "$PROFILE" -n "$NAMESPACE")
  if [[ -z ${SKIP_BUILD:-} ]]; then
    say "Building the images (the same Dockerfiles as make demo)"
    docker build -q -f backend/Dockerfile -t aiops/backend:0.1.0 . >/dev/null
    docker build -q -t aiops/web:0.1.0 frontend >/dev/null
    docker build -q -t aiops/mock-tickets-mcp:0.1.0 mcp-servers/mock-tickets-mcp >/dev/null
  fi
  say "Loading them into Minikube ($PROFILE)"
  for image in "${IMAGES[@]}"; do minikube -p "$PROFILE" image load "aiops/$image:0.1.0"; done
  say "Namespace + the Postgres password and secrets-key Secrets (random, generated once, never printed)"
  kubectl --context "$PROFILE" create namespace "$NAMESPACE" --dry-run=client -o yaml \
    | kubectl --context "$PROFILE" apply -f - >/dev/null
  if ! "${kubectl[@]}" get secret aiops-postgres >/dev/null 2>&1; then
    "${kubectl[@]}" create secret generic aiops-postgres \
      --from-literal=password="$(openssl rand -hex 24)" >/dev/null
  fi
  # Settings -> Integrations encryption key: a Fernet key is 32 random bytes, url-safe base64
  if ! "${kubectl[@]}" get secret aiops-secrets-key >/dev/null 2>&1; then
    "${kubectl[@]}" create secret generic aiops-secrets-key \
      --from-literal=AIOPS_SECRETS_KEY="$(openssl rand -base64 32 | tr '+/' '-_')" >/dev/null
  fi
  kubeconfig
  say "helm upgrade --install $RELEASE (values-minikube.yaml)"
  helm_cluster upgrade --install "$RELEASE" "$CHART" --namespace "$NAMESPACE" \
    -f values-minikube.yaml --wait --timeout 10m
  "${kubectl[@]}" get pods -o wide
  echo
  echo "Web UI:  kubectl --context $PROFILE -n $NAMESPACE port-forward svc/aiops-web 3100:3100   (fullnameOverride: aiops)"
  echo "Test:    make helm-test-minikube      Remove: make helm-uninstall-minikube"
}

case ${1:-} in
  lint) lint ;;
  template) render "${2:-}" ;;
  install-minikube) install_minikube ;;
  test-minikube) kubeconfig && helm_cluster test "$RELEASE" --namespace "$NAMESPACE" --logs ;;
  uninstall-minikube)
    kubeconfig
    helm_cluster uninstall "$RELEASE" --namespace "$NAMESPACE" --wait || true
    echo "Kept: the namespace, the aiops-postgres Secret and the Postgres volume (PVC)."
    echo "Delete them: kubectl --context $PROFILE delete namespace $NAMESPACE"
    ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
