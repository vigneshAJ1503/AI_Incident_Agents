#!/usr/bin/env bash
# Preflight: verify local prerequisites for AI Incident Agents (MASTER_PLAN.md §8).
# Exits non-zero if a REQUIRED item fails. Optional items only warn.
set -uo pipefail

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
failures=0

ok()   { printf "  ${GREEN}✅${RESET} %-22s %s\n" "$1" "$2"; }
warn() { printf "  ${YELLOW}⚠️ ${RESET} %-22s %s\n" "$1" "$2"; }
bad()  { printf "  ${RED}❌${RESET} %-22s %s\n" "$1" "$2"; failures=$((failures + 1)); }

# check_tool <name> <required|optional> <version-cmd> <install-hint>
check_tool() {
  local name=$1 level=$2 version_cmd=$3 hint=$4
  if command -v "$name" >/dev/null 2>&1; then
    ok "$name" "$(eval "$version_cmd" 2>/dev/null | head -1)"
  elif [[ $level == required ]]; then
    bad "$name" "missing → $hint"
  else
    warn "$name" "missing (optional) → $hint"
  fi
}

echo "Tools"
check_tool git        required "git --version"                 "brew install git"
check_tool uv         required "uv --version"                  "brew install uv"
check_tool docker     required "docker --version"              "brew install --cask docker"
check_tool make       required "make --version"                "xcode-select --install"
check_tool kubectl    required "kubectl version --client 2>/dev/null | head -1" "brew install kubectl"
check_tool minikube   required "minikube version --short"      "brew install minikube"
check_tool helm       optional "helm version --short"          "brew install helm"
check_tool node       required "node --version"                "brew install node@22"
check_tool pnpm       required "pnpm --version"                "brew install pnpm"
check_tool gh         optional "gh --version"                  "brew install gh"
check_tool jq         optional "jq --version"                  "brew install jq"
check_tool pre-commit optional "pre-commit --version"          "brew install pre-commit"
check_tool k9s        optional "k9s version --short"           "brew install k9s"

echo
echo "Docker"
if docker info >/dev/null 2>&1; then
  mem_bytes=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
  mem_gb=$((mem_bytes / 1024 / 1024 / 1024))
  # Lean budget (docs/setup/zero-cost.md): the full stack incl. Minikube needs ~4 GB.
  if (( mem_gb >= 5 )); then ok "docker memory" "${mem_gb} GB"
  else bad "docker memory" "${mem_gb} GB — give Docker ≥ 5 GB (Settings → Resources → Memory)"; fi
else
  bad "docker daemon" "not running — start Docker Desktop"
fi

echo
echo "Ports (must be free before infra starts)"
for port in 9200 5601 9090 9093 3000 3001 5432 6379 8000; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    warn "port $port" "in use (fine if it's our stack already running)"
  else
    ok "port $port" "free"
  fi
done

echo
echo "Configuration"
if [[ -f .env ]]; then ok ".env" "present"; else warn ".env" "missing → cp .env.example .env"; fi

echo
if (( failures > 0 )); then
  echo "${RED}${failures} required check(s) failed.${RESET}"
  exit 1
fi
echo "${GREEN}All required checks passed.${RESET}"
