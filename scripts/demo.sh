#!/usr/bin/env bash
# The one-command product demo (PR-039). Usage (via make):
#   scripts/demo.sh up      make demo        Postgres + demo data + api + web + mock-tickets-mcp (~0.3 GB)
#   scripts/demo.sh live    make demo-live   + the data stack, Minikube, MCP servers, fault injection
#   scripts/demo.sh down    make demo-down   stop everything (keeps the data)
#   scripts/demo.sh reset   make demo-reset  wipe the demo investigations + approvals, reseed, start
#   scripts/demo.sh stats   memory of the running demo
# Docs: docs/setup/demo.md. Env: NO_OPEN=1 (don't open a browser), WEB_PORT (3100),
# DEMO_SEED_MAX_AGE_H (12: re-seed when the newest demo investigation is older).
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=$([[ -f .env ]] && echo .env || echo .env.example)
INFRA=(docker compose --env-file "$ENV_FILE" -f deploy/compose/docker-compose.infra.yml)
MCP=(docker compose --env-file "$ENV_FILE" -f deploy/compose/docker-compose.mcp.yml)
APP=(docker compose --env-file "$ENV_FILE" -f deploy/compose/docker-compose.app.yml)
WEB_PORT=${WEB_PORT:-3100}
URL="http://localhost:${WEB_PORT}"
HOST_API_PID=.data/demo-api.pid
HOST_API_LOG=.data/demo-api.log

say() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }

# `aiops <args>` inside the API image: no host Python needed for the demo.
aiops_in_container() {
  "${APP[@]}" run --rm --no-deps -T --entrypoint aiops api "$@"
}

seed() {
  say "Store schema + demo history (idempotent)"
  aiops_in_container db upgrade
  aiops_in_container demo seed --if-older-than "${DEMO_SEED_MAX_AGE_H:-12}" "$@"
}

wait_http() { # url, seconds
  local url=$1 deadline=$((SECONDS + $2))
  until curl -fsS -o /dev/null "$url"; do
    if ((SECONDS > deadline)); then echo "timed out waiting for $url" >&2; return 1; fi
    sleep 1
  done
}

finish() {
  wait_http "$URL/api/health" 90
  echo
  say "The product is up: $URL"
  echo "    API (OpenAPI docs): http://localhost:${API_PORT:-8000}/api/docs"
  echo "    Stop: make demo-down   Re-seed: make demo-reset   Memory: make demo-stats"
  if [[ -z "${CI:-}" && -z "${NO_OPEN:-}" && "$(uname)" == Darwin ]]; then open "$URL" || true; fi
}

stop_host_api() {
  if [[ -f $HOST_API_PID ]]; then
    kill "$(cat "$HOST_API_PID")" 2>/dev/null || true
    rm -f "$HOST_API_PID"
  fi
}

up() {
  ./scripts/ensure-network.sh
  stop_host_api
  say "Postgres"
  "${INFRA[@]}" up -d --wait postgres
  say "Building the images (cached after the first run)"
  "${APP[@]}" build
  "${MCP[@]}" build mock-tickets-mcp
  seed "$@"
  say "mock-tickets-mcp (approved Jira drafts become tickets here)"
  "${MCP[@]}" up -d --wait mock-tickets-mcp
  say "API + Web UI"
  "${APP[@]}" up -d --wait
  finish
}

live() {
  ./scripts/ensure-network.sh
  say "Data stack (Elasticsearch, Postgres, Redis, Prometheus, Alertmanager)"
  "${INFRA[@]}" up -d --wait
  say "Minikube (2.2 GB) + sample services"
  ./scripts/minikube-up.sh
  say "MCP servers"
  make --no-print-directory mcp-up
  "${APP[@]}" build
  seed
  # Fault injection runs kubectl against the `aiops` context, so the API runs on the host here
  # (the API image has no kubectl/kubeconfig); the web container proxies to it.
  "${APP[@]}" stop api >/dev/null 2>&1 || true
  "${APP[@]}" rm -f api >/dev/null 2>&1 || true
  stop_host_api
  mkdir -p .data
  # Fault endpoints are double-guarded (PR-042): the flag AND API auth. A one-off named key
  # (never written to disk) authenticates the Web UI's server-side proxy as "demo-operator".
  local ui_key
  ui_key=${AIOPS_UI_API_KEY:-$(openssl rand -hex 24)}
  say "API on the host with AIOPS_ENABLE_FAULTS=1 + API key auth (log: $HOST_API_LOG)"
  (
    cd backend
    AIOPS_ENABLE_FAULTS=1 AIOPS_API_KEYS="demo-operator:$ui_key${AIOPS_API_KEYS:+,$AIOPS_API_KEYS}" \
      AIOPS_PROFILE=${AIOPS_PROFILE:-local-k8s} nohup uv run --no-sync \
      aiops serve --host 127.0.0.1 --port "${API_PORT:-8000}" >"../$HOST_API_LOG" 2>&1 &
    echo $! >"../$HOST_API_PID"
  )
  wait_http "http://127.0.0.1:${API_PORT:-8000}/api/health" 90
  say "Web UI -> host API"
  AIOPS_API_INTERNAL_URL=http://host.docker.internal:${API_PORT:-8000} AIOPS_UI_API_KEY="$ui_key" \
    "${APP[@]}" up -d --wait --no-deps web
  if ! grep -Eq '^OPENAI_COMPAT_API_KEY=.+' "$ENV_FILE" 2>/dev/null; then
    echo "    No LLM key in $ENV_FILE: investigations run in REPLAY mode (docs/setup/demo.md)."
  fi
  finish
}

down() {
  stop_host_api
  say "Stopping the demo (data kept)"
  "${APP[@]}" down
  "${MCP[@]}" down
  "${INFRA[@]}" --profile ui down
  if command -v minikube >/dev/null &&
    [[ "$(minikube status -p aiops --format '{{.Host}}' 2>/dev/null || true)" == Running ]]; then
    say "Stopping Minikube (kept)"
    minikube stop -p aiops
  fi
}

reset() {
  stop_host_api
  say "Wiping demo + UI-started replay investigations and the approvals volume"
  "${APP[@]}" down -v
  ./scripts/ensure-network.sh
  "${INFRA[@]}" up -d --wait postgres
  "${APP[@]}" build api
  aiops_in_container db upgrade
  aiops_in_container demo seed --reset
  up
}

stats() {
  docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}'
  docker stats --no-stream --format '{{.MemUsage}}' | awk '{
      v = $1; u = v; gsub(/[0-9.]/, "", u); gsub(/[^0-9.]/, "", v);
      m = (u == "GiB") ? v * 1024 : (u == "KiB" ? v / 1024 : v); t += m }
      END { printf "TOTAL %.0f MiB\n", t }'
}

case "${1:-up}" in
  up) shift || true; up "$@" ;;
  live) live ;;
  down) down ;;
  reset) reset ;;
  stats) stats ;;
  *) echo "usage: $0 up|live|down|reset|stats" >&2; exit 2 ;;
esac
