#!/usr/bin/env bash
# Create the shared Docker network once, with a PINNED subnet, outside docker compose.
# Compose treats it as external (so `compose down` never removes it) and Minikube joins
# it with a static IP (172.21.0.100) that therefore always stays valid.
set -euo pipefail
NETWORK=${AIOPS_NETWORK:-aiops}
SUBNET=${AIOPS_SUBNET:-172.21.0.0/16}
GATEWAY=${AIOPS_GATEWAY:-172.21.0.1}

if docker network inspect "$NETWORK" >/dev/null 2>&1; then
  current=$(docker network inspect "$NETWORK" --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}')
  if [[ "$current" != "$SUBNET" ]]; then
    echo "Network '$NETWORK' has subnet $current, expected $SUBNET." >&2
    echo "Fix once with: make infra-down mcp-down k8s-down && docker network rm $NETWORK && make infra-up" >&2
    exit 1
  fi
  exit 0
fi
docker network create --driver bridge --subnet "$SUBNET" --gateway "$GATEWAY" \
  --label aiops.managed-by=scripts/ensure-network.sh "$NETWORK" >/dev/null
echo "Created Docker network '$NETWORK' ($SUBNET)"
