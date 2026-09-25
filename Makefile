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
	@# macOS (iCloud-synced Desktop/Documents) marks files hidden; Python 3.12+ then skips .pth files
	@if [[ "$$(uname)" == Darwin ]]; then chflags -R nohidden $(BACKEND)/.venv; fi
	@if command -v pre-commit >/dev/null; then pre-commit install; else echo "pre-commit not installed: brew install pre-commit"; fi

.PHONY: lint
lint: ## Lint and format-check
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .

.PHONY: format
format: ## Auto-format code
	cd $(BACKEND) && uv run ruff check --fix . && uv run ruff format .

.PHONY: typecheck
typecheck: ## Static type checking
	cd $(BACKEND) && uv run mypy

.PHONY: test
test: ## Unit tests
	cd $(BACKEND) && uv run pytest

.PHONY: check
check: lint typecheck test ## Everything CI runs
