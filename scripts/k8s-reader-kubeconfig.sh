#!/usr/bin/env bash
# Apply the read-only RBAC (deploy/k8s/rbac) and write a kubeconfig for the aiops-reader
# ServiceAccount, with a short-lived token (kubectl create token). Never committed:
# it lands in .data/ (git-ignored). kubernetes-mcp mounts the .data/k8s DIRECTORY and
# re-reads the file when it changes (and once on a 401): no restart needed.
#
#   scripts/k8s-reader-kubeconfig.sh              always write a fresh token
#   scripts/k8s-reader-kubeconfig.sh --if-needed  only when the file is missing, the token
#                                                 is rejected by the cluster (e.g. after a
#                                                 Minikube restart), expires within
#                                                 K8S_READER_MIN_REMAINING_S (2 h), or the
#                                                 cluster CA changed
#   scripts/k8s-reader-kubeconfig.sh --check      the same test, no write (exit 1 = stale)
set -euo pipefail

CONTEXT=${K8S_ADMIN_CONTEXT:-aiops}
NAMESPACE=aiops-system
ACCOUNT=aiops-reader
DURATION=${K8S_READER_TOKEN_DURATION:-24h}
MIN_REMAINING_S=${K8S_READER_MIN_REMAINING_S:-7200}
# The API server as seen from containers on the `aiops` Docker network (Minikube node IP).
SERVER=${K8S_READER_SERVER:-https://172.21.0.100:8443}
OUT=${K8S_READER_KUBECONFIG:-.data/k8s/aiops-reader.kubeconfig}
KUBECTL=(kubectl --context "$CONTEXT")
MODE=${1:-}

cluster_ca() {
  "${KUBECTL[@]}" config view --raw --minify --flatten --context "$CONTEXT" \
    -o jsonpath='{.clusters[0].cluster.certificate-authority-data}'
}

# Why the existing kubeconfig can't be reused (empty = it is fine). Never prints the token.
stale_reason() {
  [[ -f "$OUT" ]] || { echo "missing"; return; }
  local token payload exp authenticated
  token=$(awk '$1 == "token:" {print $2; exit}' "$OUT")
  [[ -n "$token" ]] || { echo "no token in the file"; return; }
  grep -q "certificate-authority-data: $(cluster_ca)\$" "$OUT" || {
    echo "the cluster CA changed"
    return
  }
  # JWT payload (base64url) -> "exp"; tolerate a non-JWT token (then only the review counts).
  payload=$(printf '%s' "$token" | cut -d. -f2 | tr '_-' '/+')
  while (( ${#payload} % 4 )); do payload+="="; done
  exp=$(printf '%s' "$payload" | base64 -d 2>/dev/null | grep -o '"exp":[0-9]*' | cut -d: -f2 || true)
  if [[ -n "$exp" ]] && (( exp - $(date +%s) < MIN_REMAINING_S )); then
    echo "the token expires within $((MIN_REMAINING_S / 60)) min"
    return
  fi
  # Ask the API server itself (a TokenReview): catches tokens a restarted cluster rejects.
  authenticated=$(printf '{"apiVersion":"authentication.k8s.io/v1","kind":"TokenReview","spec":{"token":"%s"}}' "$token" |
    "${KUBECTL[@]}" create -f - -o jsonpath='{.status.authenticated}' 2>/dev/null || true)
  [[ "$authenticated" == "true" ]] || echo "the cluster rejects the token"
}

if [[ "$MODE" == "--if-needed" || "$MODE" == "--check" ]]; then
  reason=$(stale_reason)
  if [[ -z "$reason" ]]; then
    echo "Reader kubeconfig $OUT is valid (kept)"
    exit 0
  fi
  if [[ "$MODE" == "--check" ]]; then
    echo "Reader kubeconfig $OUT is stale: $reason (make k8s-reader-kubeconfig)"
    exit 1
  fi
  echo "Regenerating $OUT: $reason"
elif [[ -n "$MODE" ]]; then
  echo "usage: $0 [--if-needed|--check]" >&2
  exit 2
fi

"${KUBECTL[@]}" apply -k deploy/k8s/rbac >/dev/null
token=$("${KUBECTL[@]}" -n "$NAMESPACE" create token "$ACCOUNT" --duration="$DURATION")
ca=$(cluster_ca)
if [[ -z "$ca" ]]; then
  echo "Could not read the cluster CA from context '$CONTEXT'." >&2
  exit 1
fi

# Docker creates a DIRECTORY at a missing single-file bind-mount path (old compose files).
[[ -d "$OUT" ]] && rm -rf "$OUT"
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
