#!/usr/bin/env bash
# Create labels and epic milestones on GitHub (idempotent). Requires `gh auth login`.
# Usage: scripts/github/setup-repo.sh [owner/repo]
set -euo pipefail
REPO=${1:-vigneshAJ1503/AI_Incident_Agents}

labels=(
  "agent:5319e7" "mcp:1d76db" "backend:0e8a16" "frontend:fbca04" "infra:c5def5"
  "security:b60205" "observability:006b75" "testing:bfd4f2" "evaluation:d4c5f9"
  "documentation:0075ca" "bug:d73a4a" "feature:a2eeef" "refactor:cfd3d7"
  "performance:f9d0c4" "good-first-issue:7057ff" "blocked:000000" "architecture:5319e7"
)
for entry in "${labels[@]}"; do
  gh label create "${entry%%:*}" --color "${entry##*:}" --repo "$REPO" --force >/dev/null
done
echo "labels ok"

epics=(
  "EPIC-001 Platform Foundation" "EPIC-002 MCP Infrastructure" "EPIC-003 Log (ELK) Agent"
  "EPIC-004 Jira Agent" "EPIC-005 Runtime & Fault Injection" "EPIC-006 Kubernetes Agent"
  "EPIC-007 Metrics Agent" "EPIC-008 Alert Agent" "EPIC-009 Code Agent"
  "EPIC-010 Knowledge Agent" "EPIC-011 Orchestrator" "EPIC-012 RCA & Response"
  "EPIC-013 Web UI & API" "EPIC-014 Security, Evaluation & Observability" "EPIC-015 SaaS Readiness"
)
existing=$(gh api "repos/$REPO/milestones?state=all&per_page=100" --jq '.[].title')
for title in "${epics[@]}"; do
  grep -Fxq "$title" <<<"$existing" || gh api "repos/$REPO/milestones" -f title="$title" >/dev/null
done
echo "milestones ok"
