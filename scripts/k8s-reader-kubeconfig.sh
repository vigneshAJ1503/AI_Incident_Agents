#!/usr/bin/env bash
# Apply the read-only RBAC (deploy/k8s/rbac) and write a kubeconfig for the aiops-reader
# ServiceAccount, with a short-lived token (kubectl create token). Never committed:
# it lands in .data/ (git-ignored). Re-run to refresh the token; kubernetes-mcp reloads
# the file on the next 401, no restart needed.
set -euo pipefail

CONTEXT=${K8S_ADMIN_CONTEXT:-aiops}
NAMESPACE=aiops-system
ACCOUNT=aiops-reader
DURATION=${K8S_READER_TOKEN_DURATION:-24h}
# The API server as seen from containers on the `aiops` Docker network (Minikube node IP).
SERVER=${K8S_READER_SERVER:-https://172.21.0.100:8443}
OUT=${K8S_READER_KUBECONFIG:-.data/k8s/aiops-reader.kubeconfig}
KUBECTL=(kubectl --context "$CONTEXT")

"${KUBECTL[@]}" apply -k deploy/k8s/rbac >/dev/null
token=$("${KUBECTL[@]}" -n "$NAMESPACE" create token "$ACCOUNT" --duration="$DURATION")
ca=$("${KUBECTL[@]}" config view --raw --minify --flatten --context "$CONTEXT" \
  -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
if [[ -z "$ca" ]]; then
  echo "Could not read the cluster CA from context '$CONTEXT'." >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"
umask 077
tmp="$OUT.tmp"
cat >"$tmp" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: aiops
    cluster:
      server: $SERVER
      certificate-authority-data: $ca
users:
  - name: $ACCOUNT
    user:
      token: $token
contexts:
  - name: $ACCOUNT@aiops
    context: { cluster: aiops, user: $ACCOUNT, namespace: prod }
current-context: $ACCOUNT@aiops
EOF
# Readable by the container's non-root user (UID 10001) through the read-only bind mount.
chmod 644 "$tmp"
mv "$tmp" "$OUT"
echo "Wrote $OUT (ServiceAccount $NAMESPACE/$ACCOUNT, token valid $DURATION, server $SERVER)"
