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

.PHONY: check
check: lint typecheck test ## Everything CI runs

.PHONY: test-integration
test-integration: venv-fix ## Integration tests against the local stack (needs make infra-up)
	cd $(BACKEND) && uv run --no-sync pytest -m integration -o addopts=""

# --- Local infrastructure (PR-007) ---------------------------------------------------
COMPOSE := docker compose --env-file $(if $(wildcard .env),.env,.env.example) -f deploy/compose/docker-compose.infra.yml
S ?= S1

.PHONY: infra-up
infra-up: ## Start Elasticsearch, Kibana, Postgres, Redis (waits until healthy)
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

.PHONY: seed-logs
seed-logs: venv-fix ## Seed synthetic logs for a scenario: make seed-logs S=S1
	cd $(BACKEND) && uv run --no-sync aiops seed logs --scenario $(S)
