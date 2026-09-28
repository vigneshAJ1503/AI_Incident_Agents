#!/usr/bin/env bash
# pip-audit one uv project against its lockfile (runtime deps only), PR-042.
#   scripts/pip-audit.sh backend
#   scripts/pip-audit.sh mcp-servers/git-mcp
# Accepted risks: .github/security/pip-audit-ignore.txt (one vulnerability id per line,
# `# reason, owner, review date` after it). Used by .github/workflows/security.yml and
# `make audit`.
set -euo pipefail

PROJECT=${1:?usage: scripts/pip-audit.sh <project dir>}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
IGNORE_FILE="$ROOT/.github/security/pip-audit-ignore.txt"
PIP_AUDIT_VERSION=2.9.0

REQS=$(mktemp)
trap 'rm -f "$REQS"' EXIT
(cd "$ROOT/$PROJECT" && uv export --locked --no-dev --no-emit-project --no-hashes \
  --format requirements-txt -o "$REQS" >/dev/null)

IGNORES=()
if [[ -f "$IGNORE_FILE" ]]; then
  while read -r id _; do
    [[ -z "$id" || "$id" == \#* ]] && continue
    IGNORES+=(--ignore-vuln "$id")
  done <"$IGNORE_FILE"
fi

echo "pip-audit $PROJECT ($(grep -c '==' "$REQS") pinned packages)"
uvx --from "pip-audit==$PIP_AUDIT_VERSION" pip-audit -r "$REQS" --no-deps --disable-pip \
  --progress-spinner off ${IGNORES[@]+"${IGNORES[@]}"}
