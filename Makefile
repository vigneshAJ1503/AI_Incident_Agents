# AI Incident Agents — single entry point for local development.
# Run `make help` to list targets. Targets are added PR by PR (see MASTER_PLAN.md §14).

SHELL := /bin/bash
BACKEND := backend

.DEFAULT_GOAL := help

.PHONY: help
help: ## List available targets
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage: make <target>\n\n"} /^[a-zA-Z0-9_-]+:.*##/ { printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

.PHONY: preflight
preflight: ## Check local prerequisites (tools, versions, Docker memory, ports)
	@./scripts/preflight.sh

.PHONY: setup
setup: ## Install backend dependencies and git hooks
	cd $(BACKEND) && uv sync
	@$(MAKE) --no-print-directory venv-fix
	@if command -v pre-commit >/dev/null; then pre-commit install; else echo "pre-commit not installed: brew install pre-commit"; fi

.PHONY: venv-fix
venv-fix: ## macOS iCloud folders hide new files; Python 3.12+ then skips .pth files — unhide them
	@cd $(BACKEND) && uv sync -q
	@if [[ "$$(uname)" == Darwin && -d $(BACKEND)/.venv ]]; then chflags -R nohidden $(BACKEND)/.venv; fi

.PHONY: lint
lint: venv-fix ## Lint and format-check
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .

.PHONY: format
format: ## Auto-format code
	cd $(BACKEND) && uv run ruff check --fix . && uv run ruff format .

.PHONY: typecheck
typecheck: venv-fix ## Static type checking
	cd $(BACKEND) && uv run mypy

.PHONY: test
test: venv-fix ## Unit tests
	cd $(BACKEND) && uv run pytest

.PHONY: check-mcp
check-mcp: ## Lint, typecheck and test every MCP server
	@for d in mcp-servers/*/; do \
		echo "== $$d"; \
		(cd $$d && uv sync -q && { [[ "$$(uname)" != Darwin ]] || chflags -R nohidden .venv; } && \
		uv run --no-sync ruff check . && uv run --no-sync ruff format --check . && \
		uv run --no-sync mypy && uv run --no-sync pytest -q) || exit 1; \
	done

.PHONY: check
check: lint typecheck test check-mcp ## Everything CI runs

.PHONY: test-integration
test-integration: venv-fix ## Integration tests against the local stack (needs make infra-up)
	cd $(BACKEND) && uv run --no-sync pytest -m integration -o addopts=""

# --- Local infrastructure (PR-007) ---------------------------------------------------
COMPOSE := docker compose --env-file $(if $(wildcard .env),.env,.env.example) -f deploy/compose/docker-compose.infra.yml
S ?= S1

.PHONY: infra-up
infra-up: ## Start Elasticsearch, Kibana, Postgres, Redis, Alertmanager (waits until healthy)
	$(COMPOSE) --profile ui up -d --wait

.PHONY: infra-up-lite
infra-up-lite: ## Start the stack without Kibana (saves ~1 GB RAM)
	$(COMPOSE) up -d --wait

.PHONY: infra-down
infra-down: ## Stop the stack (keeps data)
	$(COMPOSE) --profile ui down

.PHONY: infra-reset
infra-reset: ## Stop the stack and DELETE its data volumes
	$(COMPOSE) --profile ui down -v

.PHONY: infra-status
infra-status: ## Show container health
	$(COMPOSE) --profile ui ps

MCP_COMPOSE := docker compose --env-file $(if $(wildcard .env),.env,.env.example) -f deploy/compose/docker-compose.mcp.yml

.PHONY: mcp-up
mcp-up: ## Build and start the MCP servers (needs make infra-up)
	$(MCP_COMPOSE) up -d --build --wait

.PHONY: mcp-down
mcp-down: ## Stop the MCP servers
	$(MCP_COMPOSE) down

.PHONY: mcp-logs
mcp-logs: ## Tail MCP server logs
	$(MCP_COMPOSE) logs -f --tail=100

.PHONY: record-fixtures
record-fixtures: venv-fix ## Re-record Log agent MCP fixtures from the live stack (needs infra-up mcp-up)
	cd $(BACKEND) && uv run --no-sync python -m tests.fixtures.record_logs
	@$(MAKE) --no-print-directory seed-logs S=S1

.PHONY: ingest-knowledge
ingest-knowledge: venv-fix ## Index knowledge-base/ runbooks into Postgres full-text search (idempotent)
	cd $(BACKEND) && uv run --no-sync aiops knowledge ingest --path ../knowledge-base

.PHONY: seed-logs
seed-logs: venv-fix ## Seed synthetic logs for a scenario: make seed-logs S=S1
	cd $(BACKEND) && uv run --no-sync aiops seed logs --scenario $(S)

# --- Evals (PR-011) ---------------------------------------------------------------------
AGENT ?= logs
MODE ?= replay

.PHONY: eval
eval: venv-fix ## Score an agent on scenarios: make eval AGENT=logs MODE=replay|live [SCENARIO=S1]
	cd $(BACKEND) && uv run --no-sync aiops eval run --agent $(AGENT) --mode $(MODE) $(if $(SCENARIO),--scenario $(SCENARIO),)

# --- Alerts (PR-023) ------------------------------------------------------------------
PROMETHEUS_IMAGE := prom/prometheus:v3.15.0
ALERTMANAGER_IMAGE := prom/alertmanager:v0.34.1

.PHONY: alertmanager-up
alertmanager-up: ## Start only Alertmanager (part of infra-up too)
	$(COMPOSE) up -d --wait alertmanager

.PHONY: seed-alerts
seed-alerts: venv-fix ## Post a scenario's firing alerts to Alertmanager: make seed-alerts S=S1
	cd $(BACKEND) && uv run --no-sync aiops seed alerts --scenario $(S)

.PHONY: check-rules
check-rules: ## Validate the alert rules (promtool check + unit tests) and alertmanager.yml (Docker)
	docker run --rm --entrypoint promtool -v "$(CURDIR)/deploy/compose/config/prometheus:/rules:ro" \
		-w /rules $(PROMETHEUS_IMAGE) check rules alert-rules.yml
	docker run --rm --entrypoint promtool -v "$(CURDIR)/deploy/compose/config/prometheus:/rules:ro" \
		-w /rules $(PROMETHEUS_IMAGE) test rules alert-rules.test.yml
	docker run --rm --entrypoint amtool -v "$(CURDIR)/deploy/compose/config/alertmanager:/cfg:ro" \
		$(ALERTMANAGER_IMAGE) check-config /cfg/alertmanager.yml

# --- Tickets (PR-012) -----------------------------------------------------------------
.PHONY: mock-tickets-up
mock-tickets-up: ## Build and start only mock-tickets-mcp (needs make infra-up)
	$(MCP_COMPOSE) up -d --build --wait mock-tickets-mcp

.PHONY: jira-mcp-up
jira-mcp-up: ## Start mcp-atlassian for a real Jira Cloud site (needs JIRA_* in .env)
	$(MCP_COMPOSE) --profile jira up -d jira-mcp

.PHONY: seed-tickets
seed-tickets: venv-fix ## Seed the mock tickets backlog (OPS project) into Postgres
	cd $(BACKEND) && uv run --no-sync aiops seed tickets
