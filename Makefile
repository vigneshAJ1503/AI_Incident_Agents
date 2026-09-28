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

.PHONY: audit
audit: ## Dependency audits like security.yml: pip-audit every Python project + npm audit (PR-042)
	@for d in backend sample-services mcp-servers/*/; do ./scripts/pip-audit.sh "$${d%/}" || exit 1; done
	cd $(UI) && npm audit --omit=dev --audit-level=high

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
	@# kubernetes-mcp bind-mounts the reader kubeconfig: if the FILE is missing when the
	@# container starts, Docker silently creates a DIRECTORY there. Create it first.
	@if [ -d .data/k8s/aiops-reader.kubeconfig ]; then \
		echo "Removing stale directory .data/k8s/aiops-reader.kubeconfig (Docker created it)"; \
		rm -rf .data/k8s/aiops-reader.kubeconfig; fi
	@if [ ! -f .data/k8s/aiops-reader.kubeconfig ] && command -v minikube >/dev/null && \
		[ "$$(minikube status -p aiops --format '{{.Host}}' 2>/dev/null)" = "Running" ]; then \
		./scripts/k8s-reader-kubeconfig.sh; fi
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

# --- Real log shipping (PR-016): Fluent Bit -> Elasticsearch logs-k8s-* -----------------
.PHONY: logging-up
logging-up: venv-fix ## Ship prod pod logs to Elasticsearch (logs-k8s-*): ES template + 2d ILM, then Fluent Bit
	cd $(BACKEND) && uv run --no-sync aiops seed k8s-logging
	$(KUBECTL) apply -k deploy/k8s/logging
	$(KUBECTL) -n logging rollout status ds/fluent-bit --timeout=180s

# --- Second logs backend (PR-P4a): Grafana Loki + loki-mcp (docs/setup/loki.md) ----------
.PHONY: loki-up
loki-up: ## Start Loki (127.0.0.1:3101, ~0.1 GB) + loki-mcp (:8110); Fluent Bit ships prod logs to it too
	@./scripts/ensure-network.sh
	$(COMPOSE) --profile loki up -d --wait loki
	$(MCP_COMPOSE) --profile loki up -d --build --wait loki-mcp

.PHONY: loki-down
loki-down: ## Stop Loki and loki-mcp (keeps the loki-data volume; 2-day retention)
	-$(MCP_COMPOSE) --profile loki stop loki-mcp
	-$(MCP_COMPOSE) --profile loki rm -f loki-mcp
	$(COMPOSE) --profile loki stop loki
	$(COMPOSE) --profile loki rm -f loki

.PHONY: loki-status
loki-status: ## Loki readiness, memory and the prod streams it holds
	@curl -fsS http://localhost:$${LOKI_PORT:-3101}/ready || true
	@docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' aiops-loki aiops-loki-mcp 2>/dev/null || true
	@curl -fsS -G http://localhost:$${LOKI_PORT:-3101}/loki/api/v1/label/app/values --data-urlencode 'query={namespace="prod"}' || true
	@echo

.PHONY: record-logs-loki
record-logs-loki: venv-fix ## Record Log agent fixtures on Loki from a LIVE fault: make record-logs-loki S=S1 (S0 = healthy)
	@if [ "$(S)" = "S0" ]; then \
		cd $(BACKEND) && uv run --no-sync python -m tests.fixtures.record_logs_loki --scenario S0 \
			--lock-repo $(CURDIR) --out $(CURDIR)/$(BACKEND)/tests/fixtures/logs-loki/S0; \
	else \
		cd $(BACKEND) && uv run --no-sync aiops fault run $(S) -- uv --directory $(CURDIR)/$(BACKEND) run --no-sync \
			python -m tests.fixtures.record_logs_loki --out $(CURDIR)/$(BACKEND)/tests/fixtures/logs-loki/$(S); \
	fi

.PHONY: logging-status
logging-status: ## Show Fluent Bit, its memory, and the logs-k8s-* indices
	$(KUBECTL) -n logging get pods -o wide
	-$(KUBECTL) -n logging top pods 2>/dev/null
	@curl -s 'http://localhost:9200/_cat/indices/logs-k8s-*?v&h=index,docs.count,store.size&s=index'

.PHONY: logging-down
logging-down: ## Remove Fluent Bit (logs-k8s-* indices stay until ILM deletes them)
	$(KUBECTL) delete -k deploy/k8s/logging --ignore-not-found

.PHONY: test-logging
test-logging: venv-fix ## Live test: fresh cluster logs reach logs-k8s-* within ~30s (needs logging-up)
	cd $(BACKEND) && uv run --no-sync pytest tests/integration/test_k8s_logging_live.py -m integration -o addopts="" -v

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

# --- K8s agent (PR-019) ------------------------------------------------------------------
.PHONY: record-k8s-fixtures
record-k8s-fixtures: venv-fix ## Re-record K8s agent fixtures LIVE: S1-S5 via aiops fault run, then S0 (~15 min)
	cd $(BACKEND) && for s in S1 S2 S3 S4 S5; do \
		uv run --no-sync aiops fault run $$s -- uv run --no-sync python -m tests.fixtures.record_k8s || exit 1; \
	done
	cd $(BACKEND) && uv run --no-sync python -m tests.fixtures.record_k8s --scenario S0

# --- Day-1 onboarding (PR-P3): aiops doctor + aiops catalog import ----------------------
PROFILE ?= local
.PHONY: doctor
doctor: venv-fix ## Check a profile end to end: make doctor PROFILE=local-k8s [ARGS="--skip-llm --json"]
	cd $(BACKEND) && uv run --no-sync aiops doctor --profile $(PROFILE) $(ARGS)

# --- Web UI (PR-036..038): frontend/ (Next.js). DEMO=1 serves the static demo dataset ------
UI := frontend
UI_PORT ?= 3100
.PHONY: ui-install
ui-install: ## Install the Web UI's pinned dependencies (npm ci)
	cd $(UI) && npm ci

.PHONY: ui-dev
ui-dev: ## Web UI dev server on http://localhost:3100 (DEMO=1: no backend needed)
	cd $(UI) && NEXT_PUBLIC_DEMO=$(if $(DEMO),1,0) npx next dev --port $(UI_PORT)

.PHONY: ui-build
ui-build: ## Production build of the Web UI (DEMO=1 bakes in demo mode)
	cd $(UI) && NEXT_PUBLIC_DEMO=$(if $(DEMO),1,0) npm run build

.PHONY: ui-test
ui-test: ## Web UI lint (ESLint + Prettier), typecheck and unit tests (Vitest)
	cd $(UI) && npm run lint && npm run typecheck && npm test

.PHONY: ui-e2e
ui-e2e: ## Web UI Playwright e2e (chromium) against a demo-mode production build
	cd $(UI) && NEXT_PUBLIC_DEMO=1 NEXT_PUBLIC_DEMO_SPEED=12 npm run build && npx playwright test

.PHONY: ui-demo-data
ui-demo-data: ## Regenerate the Web UI demo dataset (frontend/src/demo/*.json, deterministic)
	cd $(UI) && npm run demo:generate

.PHONY: ui-screenshots
ui-screenshots: ## Capture docs/ui/screenshots (dark + light) from a demo build
	cd $(UI) && NEXT_PUBLIC_DEMO=1 npm run build && SCREENSHOTS=1 npx playwright test screenshots

# Visual-regression baselines are Linux-only (CI compares them), so regenerate them in the same
# pinned Playwright image; node_modules and .next stay in container volumes (no host clobbering).
PLAYWRIGHT_IMAGE := mcr.microsoft.com/playwright:v1.63.0-noble
.PHONY: ui-visual-update
ui-visual-update: ## Regenerate frontend/e2e/__screenshots__ (visual baselines) in the Playwright container
	docker run --rm --memory 2g --ipc=host -v "$(CURDIR)/$(UI):/work" -v /work/node_modules \
	  -v /work/.next -w /work -e CI=1 -e VISUAL=1 -e NEXT_TELEMETRY_DISABLED=1 $(PLAYWRIGHT_IMAGE) \
	  sh -c 'npm ci --no-audit --no-fund && NEXT_PUBLIC_DEMO=1 NEXT_PUBLIC_DEMO_SPEED=12 npm run build && npx playwright test visual --update-snapshots=all'

# --- Orchestrator demo data (PR-034) -------------------------------------------------
.PHONY: demo-seed
demo-seed: ## Store S0-S5 replay investigations + a 14-day synthetic history (zero tokens)
	cd $(BACKEND) && uv run aiops demo seed

.PHONY: demo-export
demo-export: ## Export contract-shaped demo JSON from real replays (default demo/export)
	cd $(BACKEND) && uv run aiops demo export --out $(abspath $(or $(OUT),demo/export))

.PHONY: eval-investigations
eval-investigations: ## Replay S0-S5 end to end and score the RCA against ground truth
	cd $(BACKEND) && uv run aiops eval investigations

# --- System evaluation (PR-040): docs/evals.md ------------------------------------------
.PHONY: evaluate
evaluate: venv-fix ## ONE scorecard: every agent + planner + S0-S5 end to end (replay, zero tokens) + regression gate [SCENARIO=S1] [EVALUATE_ARGS=...]
	cd $(BACKEND) && uv run --no-sync aiops evaluate --mode replay $(if $(SCENARIO),--scenario $(SCENARIO),) $(EVALUATE_ARGS)

.PHONY: evaluate-live
evaluate-live: venv-fix ## Same on the live stack with the configured hosted LLM (spends tokens) [JUDGE=1] [EVALUATE_ARGS=...]
	cd $(BACKEND) && uv run --no-sync aiops evaluate --mode live $(if $(JUDGE),--judge,) $(EVALUATE_ARGS)

.PHONY: evaluate-baseline
evaluate-baseline: venv-fix ## Accept the current replay numbers as the new baseline (evals/baselines/replay.json)
	cd $(BACKEND) && uv run --no-sync aiops evaluate --mode replay --update-baseline

# --- REST + SSE API (PR-035): `aiops serve`, docs/api/README.md ----------------------------
APP_COMPOSE := docker compose --env-file $(if $(wildcard .env),.env,.env.example) -f deploy/compose/docker-compose.app.yml
API_PORT ?= 8000

.PHONY: api
api: venv-fix ## Run the API on http://127.0.0.1:8000/api (docs /api/docs; RELOAD=1 for --reload)
	cd $(BACKEND) && uv run --no-sync aiops serve --host 127.0.0.1 --port $(API_PORT) $(if $(RELOAD),--reload,)

.PHONY: api-up
api-up: ## Build and start the API (127.0.0.1:8000) + Web UI (127.0.0.1:3100) containers
	@./scripts/ensure-network.sh
	$(COMPOSE) up -d --wait postgres
	$(APP_COMPOSE) up -d --build --wait

.PHONY: api-down
api-down: ## Stop the API + Web UI containers
	$(APP_COMPOSE) down

.PHONY: api-logs
api-logs: ## Tail the API container's JSON logs
	$(APP_COMPOSE) logs -f --tail=100

# --- The one-command demo (PR-039): docs/setup/demo.md ----------------------------------
.PHONY: demo
demo: ## THE demo: Postgres + seeded history + API + Web UI + mock tickets on http://localhost:3100 (~0.3 GB)
	@./scripts/demo.sh up

.PHONY: demo-live
demo-live: ## Full stack: data stack + Minikube + MCP servers + fault injection from the Scenarios page (~2.9 GB)
	@./scripts/demo.sh live

.PHONY: demo-down
demo-down: ## Stop everything the demo started, incl. Minikube (keeps the data)
	@./scripts/demo.sh down

.PHONY: demo-reset
demo-reset: ## Wipe the demo investigations + approvals, reseed and start again
	@./scripts/demo.sh reset

.PHONY: demo-stats
demo-stats: ## Memory of the running containers (docker stats) and the total
	@./scripts/demo.sh stats

.PHONY: demo-e2e
demo-e2e: ## Playwright against the REAL API (needs make demo): seeded history, report, S1 replay, approval
	cd $(UI) && E2E_BASE_URL=http://localhost:$(UI_PORT) npx playwright test -c playwright.real.config.ts
