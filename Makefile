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

.PHONY: check-sample
check-sample: ## Lint, typecheck and test the sample services
	@cd sample-services && uv sync -q && { [[ "$$(uname)" != Darwin ]] || chflags -R nohidden .venv; } && \
		uv run --no-sync ruff check . && uv run --no-sync ruff format --check . && \
		uv run --no-sync mypy && uv run --no-sync pytest -q

.PHONY: check-mcp
check-mcp: ## Lint, typecheck and test every MCP server
	@for d in mcp-servers/*/; do \
		echo "== $$d"; \
		(cd $$d && uv sync -q && { [[ "$$(uname)" != Darwin ]] || chflags -R nohidden .venv; } && \
		uv run --no-sync ruff check . && uv run --no-sync ruff format --check . && \
		uv run --no-sync mypy && uv run --no-sync pytest -q) || exit 1; \
	done

.PHONY: check
check: lint typecheck test check-mcp check-sample ## Everything CI runs

.PHONY: test-integration
test-integration: venv-fix ## Integration tests against the local stack (needs make infra-up)
	cd $(BACKEND) && uv run --no-sync pytest -m integration -o addopts=""

# --- Local infrastructure (PR-007) ---------------------------------------------------
COMPOSE := docker compose --env-file $(if $(wildcard .env),.env,.env.example) -f deploy/compose/docker-compose.infra.yml
S ?= S1

.PHONY: infra-up
infra-up: ## Start the lean data stack: Elasticsearch, Postgres, Redis, Alertmanager, Prometheus (no UIs)
	@./scripts/ensure-network.sh
	$(COMPOSE) up -d --wait

.PHONY: ui-up
ui-up: ## Start the optional UIs (Kibana ~0.6 GB, Grafana ~0.1 GB); agents never need them
	$(COMPOSE) --profile ui up -d --wait kibana grafana

.PHONY: grafana-up
grafana-up: ## Start only Grafana (http://localhost:3000, dashboards "Service Overview", "K8s Workloads")
	$(COMPOSE) --profile ui up -d --wait grafana

.PHONY: ui-down
ui-down: ## Stop the optional UIs to free memory
	$(COMPOSE) --profile ui stop kibana grafana

.PHONY: infra-up-lite
infra-up-lite: ## Alias of infra-up (kept for compatibility)
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

.PHONY: record-knowledge-fixtures
record-knowledge-fixtures: venv-fix ## Re-record Knowledge agent fixtures (needs infra-up ingest-knowledge + knowledge-mcp)
	cd $(BACKEND) && uv run --no-sync python -m tests.fixtures.record_knowledge

.PHONY: ingest-knowledge
ingest-knowledge: venv-fix ## Index knowledge-base/ runbooks into Postgres full-text search (idempotent)
	cd $(BACKEND) && uv run --no-sync aiops knowledge ingest --path ../knowledge-base

# --- Local Kubernetes (PR-015) ---------------------------------------------------------
KUBECTL := kubectl --context aiops

.PHONY: k8s-up
k8s-up: ## Start the lean Minikube cluster (2.2 GB) and deploy the sample services
	./scripts/minikube-up.sh

.PHONY: monitoring-up
monitoring-up: ## Deploy kube-state-metrics into the cluster (NodePort 30080; part of k8s-up)
	$(KUBECTL) apply -k deploy/k8s/monitoring
	$(KUBECTL) -n monitoring rollout status deployment/kube-state-metrics --timeout=180s

.PHONY: k8s-status
k8s-status: ## Show sample-service pods and the cluster's memory use
	$(KUBECTL) -n prod get pods -o wide
	@docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' aiops

.PHONY: k8s-logs
k8s-logs: ## Tail a sample service's logs: make k8s-logs SVC=payment-service
	$(KUBECTL) -n prod logs -f deploy/$(or $(SVC),payment-service) --tail=50

.PHONY: k8s-down
k8s-down: ## Stop the cluster (keeps it; frees its memory)
	minikube stop -p aiops

.PHONY: k8s-delete
k8s-delete: ## DELETE the cluster entirely
	minikube delete -p aiops

# --- Fault injection (PR-017): one scenario at a time, guarded by .data/cluster.lock ---
.PHONY: inject-fault
inject-fault: venv-fix ## Inject a live incident: make inject-fault S=S1 (or TYPE=db-timeout)
	cd $(BACKEND) && uv run --no-sync aiops fault inject $(or $(TYPE),$(S))

.PHONY: revert-fault
revert-fault: venv-fix ## Restore the healthy baseline in the cluster
	cd $(BACKEND) && uv run --no-sync aiops fault revert

.PHONY: test-faults
test-faults: venv-fix ## Live fault-injection tests S1-S5 against Minikube (~12 min; needs k8s-up)
	cd $(BACKEND) && uv run --no-sync pytest -m faults -o addopts="" -v

.PHONY: fault-status
fault-status: venv-fix ## Show which scenario (if any) is active
	cd $(BACKEND) && uv run --no-sync aiops fault status

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

.PHONY: prometheus-up
prometheus-up: ## Start only Prometheus (http://localhost:9090; part of infra-up)
	$(COMPOSE) up -d --wait prometheus

.PHONY: prometheus-mcp-up
prometheus-mcp-up: ## Build and start only prometheus-mcp on 127.0.0.1:8103 (the metrics capability)
	$(MCP_COMPOSE) up -d --build --wait prometheus-mcp

.PHONY: record-metrics
record-metrics: venv-fix ## Record Metrics agent fixtures from a LIVE fault: make record-metrics S=S1 (S0 = healthy, no fault)
	@if [[ "$(S)" == S0 ]]; then \
		cd $(BACKEND) && uv run --no-sync python -m tests.fixtures.record_metrics --scenario S0; \
	else \
		cd $(BACKEND) && uv run --no-sync aiops fault run $(S) -- uv run --no-sync python -m tests.fixtures.record_metrics; \
	fi

.PHONY: prometheus-reload
prometheus-reload: ## Reload prometheus.yml / alert-rules.yml without a restart (SIGHUP)
	docker kill -s HUP aiops-prometheus

.PHONY: alertmanager-up
alertmanager-up: ## Start only Alertmanager (part of infra-up too)
	$(COMPOSE) up -d --wait alertmanager

.PHONY: seed-alerts
seed-alerts: venv-fix ## Post a scenario's firing alerts to Alertmanager: make seed-alerts S=S1
	cd $(BACKEND) && uv run --no-sync aiops seed alerts --scenario $(S)

.PHONY: record-fixtures-alerts
record-fixtures-alerts: venv-fix ## Re-record Alert agent fixtures (needs alertmanager-up + alertmanager-mcp)
	cd $(BACKEND) && uv run --no-sync python -m tests.fixtures.record_alerts

.PHONY: check-rules
check-rules: ## Validate prometheus.yml, the alert rules (check + unit tests) and alertmanager.yml (Docker)
	docker run --rm --entrypoint promtool \
		-v "$(CURDIR)/deploy/compose/config/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
		-v "$(CURDIR)/deploy/compose/config/prometheus/alert-rules.yml:/etc/prometheus/rules/alert-rules.yml:ro" \
		$(PROMETHEUS_IMAGE) check config /etc/prometheus/prometheus.yml
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

.PHONY: record-tickets-fixtures
record-tickets-fixtures: venv-fix ## Re-record Tickets agent fixtures from mock-tickets-mcp (reseeds at FIXED_NOW)
	cd $(BACKEND) && uv run --no-sync python -m tests.fixtures.record_tickets

.PHONY: seed-repo
seed-repo: venv-fix ## Build the sample Git repo (.data/sample-repo) for a scenario: make seed-repo S=S1
	cd $(BACKEND) && uv run --no-sync aiops seed repo --scenario $(S) $(if $(NOW),--now $(NOW),)

.PHONY: git-mcp-up
git-mcp-up: ## Build and start only git-mcp on 127.0.0.1:8107 (mounts .data/sample-repo read-only)
	$(MCP_COMPOSE) up -d --build --wait git-mcp

.PHONY: record-code
record-code: venv-fix ## Re-record Code agent fixtures from the live git-mcp (needs git-mcp-up)
	cd $(BACKEND) && uv run --no-sync python -m tests.fixtures.record_code
	@$(MAKE) --no-print-directory seed-repo S=S1

# --- Kubernetes MCP (PR-018): read-only ServiceAccount + kubernetes-mcp ---------------
.PHONY: k8s-rbac
k8s-rbac: ## Apply the read-only RBAC for the agents (aiops-system/aiops-reader)
	$(KUBECTL) apply -k deploy/k8s/rbac

.PHONY: k8s-reader-kubeconfig
k8s-reader-kubeconfig: ## RBAC + a short-lived read-only kubeconfig in .data/k8s (rerun when the token expires)
	./scripts/k8s-reader-kubeconfig.sh

.PHONY: kubernetes-mcp-up
kubernetes-mcp-up: ## Build and start only kubernetes-mcp on 127.0.0.1:8106 (needs k8s-reader-kubeconfig)
	@test -f .data/k8s/aiops-reader.kubeconfig || ./scripts/k8s-reader-kubeconfig.sh
	$(MCP_COMPOSE) up -d --build --wait kubernetes-mcp

.PHONY: k8s-can-i
k8s-can-i: ## Show what the read-only ServiceAccount may do in prod (no writes, secrets, exec)
	$(KUBECTL) auth can-i --list -n prod --as=system:serviceaccount:aiops-system:aiops-reader
