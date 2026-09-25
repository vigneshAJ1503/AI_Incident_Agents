# AI SRE Incident Investigation Platform

## Master Engineering Plan --- End-to-End Product Development, PR by PR

**Document status:** Master implementation plan\
**Target:** Local-first, production-oriented SaaS prototype\
**Primary goal:** Build an AI-powered incident investigation platform
that receives an incident/ticket, gathers evidence from logs,
Kubernetes, metrics, alerts, source control and knowledge systems
through MCP, correlates the evidence, produces an evidence-backed RCA
hypothesis, and optionally performs controlled remediation.

------------------------------------------------------------------------

# 1. Product Vision

Build a SaaS platform where an engineer can provide an incident such as:

> "Payment API is returning HTTP 500 in production."

The platform automatically:

1.  Understands the incident.
2.  Identifies the affected service/environment/time range.
3.  Creates an investigation plan.
4.  Delegates work to specialized agents.
5.  Queries infrastructure through MCP tools.
6.  Correlates logs, metrics, Kubernetes state, alerts and code changes.
7.  Produces an evidence-backed root-cause hypothesis.
8.  Shows the evidence and reasoning trail to the engineer.
9.  Recommends next steps.
10. Optionally creates/updates a Jira incident.
11. Eventually supports human-approved remediation.

The platform must be **evidence-first** rather than an LLM that freely
guesses an answer.

------------------------------------------------------------------------

# 2. Core Architecture

``` text
                         ┌──────────────────────────────┐
                         │          WEB UI / API        │
                         │ React / Next.js / REST API   │
                         └──────────────┬───────────────┘
                                        │
                                        ▼
                         ┌──────────────────────────────┐
                         │      ORCHESTRATOR AGENT      │
                         │                              │
                         │ Intent → Plan → Delegate     │
                         │ → Correlate → Final Response │
                         └──────────────┬───────────────┘
                                        │
             ┌──────────────────────────┼──────────────────────────┐
             │                          │                          │
             ▼                          ▼                          ▼
      ┌─────────────┐            ┌─────────────┐            ┌─────────────┐
      │  ELK Agent  │            │  K8s Agent  │            │ Code Agent  │
      └──────┬──────┘            └──────┬──────┘            └──────┬──────┘
             │                          │                          │
             ▼                          ▼                          ▼
      Elasticsearch                Kubernetes                    Git
          MCP                         MCP                         MCP

             ┌──────────────────────────┼──────────────────────────┐
             │                          │                          │
             ▼                          ▼                          ▼
      Metrics Agent              Alert Agent                Knowledge Agent
             │                          │                          │
             ▼                          ▼                          ▼
       Prometheus MCP             Alertmanager MCP          Docs / Runbooks

                                        │
                                        ▼
                              ┌────────────────────┐
                              │   RCA / Correlation │
                              │        Agent        │
                              └──────────┬─────────┘
                                         │
                                         ▼
                              ┌────────────────────┐
                              │ Response Builder   │
                              └──────────┬─────────┘
                                         │
                                         ▼
                              Evidence + RCA + Next
                              Steps + Links + Actions
```

------------------------------------------------------------------------

# 3. Important Architectural Principles

## 3.1 Agent vs MCP

An **agent owns reasoning**.

An **MCP server owns controlled access to an external system**.

Do not put Elasticsearch credentials and REST API logic directly inside
the LLM agent.

Prefer:

``` text
ELK Agent
    ↓
MCP Client
    ↓
Elasticsearch MCP Server
    ↓
Elasticsearch
```

Similarly:

``` text
K8s Agent
    ↓
Kubernetes MCP
    ↓
Kubernetes API
```

and:

``` text
Code Agent
    ↓
Git MCP
    ↓
GitHub / Git repository
```

------------------------------------------------------------------------

# 4. Recommended Repository

Use a monorepo initially.

``` text
ai-sre-platform/
│
├── apps/
│   ├── web/
│   └── api/
│
├── services/
│   ├── orchestrator/
│   ├── elk-agent/
│   ├── k8s-agent/
│   ├── metrics-agent/
│   ├── alert-agent/
│   ├── code-agent/
│   ├── knowledge-agent/
│   ├── rca-agent/
│   └── ticket-agent/
│
├── packages/
│   ├── agent-core/
│   ├── mcp-client/
│   ├── schemas/
│   ├── observability/
│   └── config/
│
├── infrastructure/
│   ├── docker/
│   ├── kubernetes/
│   ├── elasticsearch/
│   ├── prometheus/
│   ├── grafana/
│   └── alertmanager/
│
├── mcp/
│   ├── elasticsearch/
│   ├── kubernetes/
│   ├── prometheus/
│   ├── github/
│   └── jira/
│
├── sample-services/
│   ├── payment-service/
│   ├── order-service/
│   └── user-service/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   └── e2e/
│
├── docs/
│   ├── architecture/
│   ├── agents/
│   ├── mcp/
│   ├── runbooks/
│   └── adr/
│
├── scripts/
│
├── docker-compose.yml
├── Makefile
├── README.md
├── CONTRIBUTING.md
├── SECURITY.md
└── .github/
    ├── workflows/
    ├── ISSUE_TEMPLATE/
    └── pull_request_template.md
```

------------------------------------------------------------------------

# 5. Technology Baseline

## Backend

Recommended:

-   Python for initial agent/orchestration implementation.
-   FastAPI for API layer.
-   Pydantic for schemas.
-   AsyncIO for concurrent tool calls.
-   PostgreSQL for application state.
-   Redis for caching and short-lived execution state.

## AI

Abstract the LLM behind:

``` text
LLMProvider
├── OpenAIProvider
├── GroqProvider
└── LocalProvider
```

The application should never directly depend on one vendor SDK
throughout the codebase.

## Observability

-   Elasticsearch
-   Kibana
-   Prometheus
-   Grafana
-   Alertmanager
-   OpenTelemetry where practical

## Infrastructure

-   Docker Compose for the first local environment.
-   Kind or K3d for Kubernetes testing.
-   Terraform later for cloud environments.

## Frontend

-   Next.js / React
-   TypeScript
-   Tailwind or another maintainable design system

------------------------------------------------------------------------

# 6. Standard PR Workflow

Every PR should follow:

``` text
Issue
  ↓
Feature branch
  ↓
Implementation
  ↓
Unit tests
  ↓
Integration tests where applicable
  ↓
Documentation
  ↓
Pull Request
  ↓
CI
  ↓
Code review
  ↓
Merge
```

Recommended branch names:

``` text
feat/001-project-bootstrap
feat/002-config-system
feat/003-elk-mcp
feat/004-elk-agent
fix/xxx-description
refactor/xxx-description
test/xxx-description
docs/xxx-description
```

Recommended commit style:

``` text
feat(elk): add elasticsearch MCP client
fix(orchestrator): prevent duplicate investigation steps
test(elk): add query builder tests
docs(architecture): document agent boundaries
```

------------------------------------------------------------------------

# 7. PR Roadmap

The implementation is intentionally incremental.

Do not build all agents simultaneously.

------------------------------------------------------------------------

# PR 001 --- Repository Bootstrap

## Goal

Create the base repository and engineering standards.

## Work

Create:

``` text
README.md
CONTRIBUTING.md
SECURITY.md
LICENSE
.gitignore
.env.example
Makefile
```

Create the directory structure.

Add formatting/linting.

Python:

-   Ruff
-   MyPy
-   Pytest

Frontend:

-   ESLint
-   Prettier
-   TypeScript

## Acceptance Criteria

-   Repository clones successfully.
-   Backend starts.
-   Frontend starts.
-   Test command works.
-   Lint command works.
-   CI workflow runs.

------------------------------------------------------------------------

# PR 002 --- CI/CD Foundation

## Goal

Make GitHub the source of truth for quality gates.

## GitHub Actions

Create:

``` text
.github/workflows/
├── ci.yml
├── security.yml
└── build.yml
```

CI should run:

``` text
lint
type-check
unit-tests
build
```

Later add:

``` text
integration-tests
container-build
image-scan
```

## Acceptance Criteria

A PR cannot merge if:

``` text
lint fails
OR
tests fail
OR
build fails
```

------------------------------------------------------------------------

# PR 003 --- Application Configuration System

## Goal

Centralize configuration.

Example:

``` yaml
services:
  payment-service:
    environments:
      production:
        elasticsearch_index: payment-prod-*

elasticsearch:
  url: ${ELASTICSEARCH_URL}

llm:
  provider: ${LLM_PROVIDER}
  model: ${LLM_MODEL}
```

Never commit secrets.

Use:

``` text
.env
.env.example
```

## Acceptance Criteria

-   Configuration loads from environment.
-   Invalid configuration fails fast.
-   Secrets are never logged.
-   Service-to-index mapping works.

------------------------------------------------------------------------

# PR 004 --- Domain Models

## Goal

Create stable schemas before implementing agents.

Important objects:

``` text
Incident
Investigation
InvestigationStep
AgentTask
AgentResult
Evidence
Finding
Hypothesis
Recommendation
ToolCall
```

Example:

``` python
class Incident(BaseModel):
    id: str
    title: str
    description: str
    environment: str | None
    service: str | None
    created_at: datetime
```

Agent result:

``` python
class AgentResult(BaseModel):
    agent: str
    status: Literal["success", "partial", "failed"]
    findings: list[Finding]
    evidence: list[Evidence]
    confidence: float | None
```

------------------------------------------------------------------------

# PR 005 --- MCP Client Framework

## Goal

Create one reusable MCP client abstraction.

``` text
Agent
  ↓
MCPClient
  ↓
MCP Server
```

The agent should not care whether the MCP server runs:

-   locally
-   in Docker
-   remotely

Define:

``` python
class MCPClient:
    async def list_tools(...)
    async def call_tool(...)
```

Add:

-   connection lifecycle
-   timeout
-   retry
-   structured errors
-   request IDs
-   audit logging

------------------------------------------------------------------------

# PR 006 --- Local Elasticsearch + Kibana

## Goal

Create the local logging environment.

Docker Compose:

``` text
elasticsearch
kibana
```

Create:

``` text
payment-prod-*
order-prod-*
user-prod-*
```

Seed synthetic logs.

Example:

``` json
{
  "timestamp": "...",
  "service": "payment-service",
  "environment": "production",
  "level": "ERROR",
  "status_code": 500,
  "endpoint": "/api/payment",
  "trace_id": "trace-123",
  "message": "Database connection timeout"
}
```

------------------------------------------------------------------------

# PR 007 --- Elasticsearch MCP Integration

## Goal

Connect the platform to Elasticsearch through MCP.

Required tools:

``` text
list_indices
get_mapping
search_logs
execute_esql
```

Avoid exposing unrestricted arbitrary commands initially.

## Security

Prefer read-only credentials.

Enforce:

``` text
allowed indices
maximum time range
maximum result size
query timeout
```

------------------------------------------------------------------------

# PR 008 --- ELK Agent v1

## Goal

Build the first real agent.

Input:

``` json
{
  "incident_id": "INC-1024",
  "description": "Payment API is returning 500",
  "service": "payment-service",
  "environment": "production",
  "time_range": "30m"
}
```

Agent flow:

``` text
Understand request
      ↓
Resolve service
      ↓
Resolve index
      ↓
Build query
      ↓
Search logs
      ↓
Analyze results
      ↓
Produce structured findings
```

Output:

``` json
{
  "agent": "elk-agent",
  "status": "success",
  "findings": [
    {
      "type": "error_pattern",
      "description": "Database connection timeout"
    }
  ],
  "evidence": [
    {
      "source": "elasticsearch",
      "query": "...",
      "trace_ids": ["trace-123"]
    }
  ]
}
```

------------------------------------------------------------------------

# PR 009 --- ELK Investigation Loop

## Goal

Move beyond one query.

The agent should be able to perform:

``` text
Query 1
  ↓
Analyze
  ↓
Need more evidence?
  ↓ yes
Query 2
  ↓
Analyze
  ↓
Need trace correlation?
  ↓ yes
Query 3
```

Example:

``` text
HTTP 500
 ↓
Find top exception
 ↓
Find trace IDs
 ↓
Find related service
 ↓
Find first occurrence
 ↓
Compare before/after incident
```

Add limits:

``` text
max_tool_calls = 10
max_execution_time = 60s
max_results_per_query = 1000
```

------------------------------------------------------------------------

# PR 010 --- ELK Agent Evaluation Suite

## Goal

Test the agent against deterministic incidents.

Create datasets:

``` text
scenario-db-timeout
scenario-memory-leak
scenario-http-500
scenario-network-timeout
scenario-auth-failure
scenario-noise-only
scenario-no-root-cause
```

Measure:

``` text
service identification
query correctness
finding correctness
evidence quality
false-positive rate
latency
token usage
```

------------------------------------------------------------------------

# PR 011 --- Local Kubernetes Environment

## Goal

Create a realistic application environment.

Use Kind/K3d.

Deploy:

``` text
payment-service
order-service
user-service
redis
postgres
```

Each service should expose:

``` text
/health
/ready
/metrics
```

------------------------------------------------------------------------

# PR 012 --- Kubernetes MCP

## Goal

Expose read-only Kubernetes operations.

Tools:

``` text
list_pods
get_pod
list_deployments
get_deployment
list_events
get_service
get_logs
```

Initially prohibit:

``` text
delete
scale
restart
patch
apply
exec
```

------------------------------------------------------------------------

# PR 013 --- Kubernetes Agent

## Goal

Investigate runtime health.

Input:

``` json
{
  "service": "payment-service",
  "environment": "production",
  "time_range": "30m"
}
```

Output:

``` json
{
  "agent": "k8s-agent",
  "findings": [
    "Pod restarted 17 times",
    "Container entered CrashLoopBackOff"
  ],
  "evidence": [
    "pod/payment-service-7d9c8",
    "event/BackOff"
  ]
}
```

------------------------------------------------------------------------

# PR 014 --- Prometheus + Grafana

## Goal

Add metrics.

Metrics:

``` text
http_requests_total
http_request_duration_seconds
http_5xx_total
process_cpu_seconds_total
process_resident_memory_bytes
```

------------------------------------------------------------------------

# PR 015 --- Prometheus MCP

## Goal

Expose metrics queries.

Tools:

``` text
query
query_range
get_targets
```

Add query safety:

``` text
query timeout
range limit
allowed expressions
```

------------------------------------------------------------------------

# PR 016 --- Metrics Agent

## Goal

Detect:

``` text
traffic spike
latency spike
error-rate spike
CPU saturation
memory pressure
```

Output must contain evidence:

``` json
{
  "metric": "http_5xx_rate",
  "baseline": 0.02,
  "current": 0.25,
  "window": "10m"
}
```

------------------------------------------------------------------------

# PR 017 --- Alertmanager Integration

## Goal

Bring alerts into the investigation.

Agent capabilities:

``` text
list active alerts
get alert details
search alert history
correlate alert timestamp
```

------------------------------------------------------------------------

# PR 018 --- Alert Agent

## Goal

Determine whether the incident correlates with known alerts.

Example:

``` text
Incident started: 10:30

Alert:
DatabaseConnectionPoolExhausted

Started: 10:29
```

This becomes evidence, not an automatic root cause.

------------------------------------------------------------------------

# PR 019 --- Local Git Repository

## Goal

Create realistic source history.

Create commits such as:

``` text
feat: add payment connection pooling
fix: increase DB timeout
refactor: payment repository
chore: upgrade dependencies
```

Introduce deliberately faulty changes for testing.

------------------------------------------------------------------------

# PR 020 --- Git/GitHub MCP

## Goal

Expose:

``` text
search commits
get commit
get diff
list releases
search pull requests
```

Keep repository access scoped.

------------------------------------------------------------------------

# PR 021 --- Code Agent

## Goal

Correlate runtime symptoms with source changes.

Input:

``` json
{
  "service": "payment-service",
  "symptoms": [
    "database timeout",
    "HTTP 500"
  ],
  "time_range": "1h"
}
```

Output:

``` text
Recent deployment:
v1.8.2

Relevant commit:
abc123

Changed:
database connection pool configuration
```

------------------------------------------------------------------------

# PR 022 --- Knowledge / Runbook Agent

## Goal

Allow the platform to search internal operational knowledge.

Sources:

``` text
runbooks
architecture docs
incident documentation
service ownership
known issues
```

The agent should return citations/references.

------------------------------------------------------------------------

# PR 023 --- Jira Agent

## Goal

Integrate incident management.

Operations:

``` text
get_issue
search_issues
add_comment
update_issue
create_issue
```

Start with read-only operations.

Write operations require explicit user approval.

------------------------------------------------------------------------

# PR 024 --- Orchestrator v1

## Goal

Connect the agents.

Input:

``` text
"Payment API is failing in production."
```

Orchestrator extracts:

``` json
{
  "service": "payment-service",
  "environment": "production",
  "symptom": "HTTP 500",
  "time_range": "1h"
}
```

Then creates:

``` text
Investigation Plan
```

Example:

``` text
1. Search ELK
2. Check Kubernetes
3. Check metrics
4. Check alerts
5. Check recent code changes
6. Correlate findings
```

------------------------------------------------------------------------

# PR 025 --- Parallel Agent Execution

## Goal

Run independent agents concurrently.

Instead of:

``` text
ELK
 ↓
K8s
 ↓
Metrics
 ↓
Git
```

use:

``` text
              Orchestrator
              /    |    \
             /     |     \
           ELK     K8s    Metrics
             \      |      /
              \     |     /
               Git / Alert
```

Use async execution.

Conceptually:

``` python
results = await asyncio.gather(
    elk_agent.investigate(task),
    k8s_agent.investigate(task),
    metrics_agent.investigate(task),
    alert_agent.investigate(task),
    code_agent.investigate(task),
)
```

------------------------------------------------------------------------

# PR 026 --- Evidence Store

## Goal

Persist investigation evidence.

Store:

``` text
incident
investigation
agent execution
tool call
query
finding
evidence
hypothesis
recommendation
```

Do not store arbitrary raw logs indefinitely.

Store references and relevant excerpts.

------------------------------------------------------------------------

# PR 027 --- Correlation / RCA Agent

## Goal

Correlate all findings.

Input:

``` text
ELK findings
K8s findings
Metrics findings
Alert findings
Code findings
Knowledge findings
```

Output:

``` json
{
  "hypothesis": "Database connection pool misconfiguration",
  "confidence": 0.91,
  "supporting_evidence": [
    "...",
    "...",
    "..."
  ],
  "contradicting_evidence": [],
  "recommended_next_steps": [
    "Compare v1.8.2 with v1.8.1",
    "Validate connection pool configuration",
    "Review database saturation"
  ]
}
```

Important:

The RCA agent should distinguish:

``` text
FACT
OBSERVATION
CORRELATION
HYPOTHESIS
RECOMMENDATION
```

Never present a hypothesis as a verified fact.

------------------------------------------------------------------------

# PR 028 --- Response Builder

## Goal

Create the final user-facing incident report.

Structure:

``` text
Incident
────────

Impact

Timeline

Key Findings

Logs

Metrics

Kubernetes State

Recent Changes

Root Cause Hypothesis

Confidence

Supporting Evidence

Contradicting Evidence

Recommended Next Steps

Links
```

------------------------------------------------------------------------

# PR 029 --- Investigation Timeline

## Goal

Build a chronological timeline.

Example:

``` text
10:20  Deployment v1.8.2
10:27  DB connection pool change active
10:29  DB latency increases
10:30  5xx rate increases
10:31  Pods restart
10:32  Alert triggered
10:35  Incident created
```

This will significantly improve human debugging.

------------------------------------------------------------------------

# PR 030 --- Web UI v1

## Screens

``` text
Dashboard
Incidents
Investigation
Agents
Evidence
Services
Settings
```

Incident page:

``` text
┌──────────────────────────────────────────┐
│ INC-1024                                 │
│ Payment API failing                      │
├──────────────────────────────────────────┤
│ Status: Investigating                    │
│ Service: payment-service                 │
│ Environment: production                  │
├──────────────────────────────────────────┤
│ Agent Execution                          │
│ ✓ ELK                                   │
│ ✓ Kubernetes                            │
│ ✓ Metrics                               │
│ ✓ Alerts                                │
│ ✓ Code                                  │
│ ● RCA                                   │
├──────────────────────────────────────────┤
│ Root Cause Hypothesis                    │
│ Database connection pool configuration   │
│ Confidence: 91%                          │
├──────────────────────────────────────────┤
│ Evidence | Logs | Metrics | K8s | Git   │
└──────────────────────────────────────────┘
```

------------------------------------------------------------------------

# PR 031 --- Streaming Agent Execution

## Goal

Show live execution.

Example:

``` text
[10:32:01] Incident received
[10:32:02] Orchestrator created plan
[10:32:02] ELK Agent started
[10:32:02] K8s Agent started
[10:32:02] Metrics Agent started
[10:32:03] Alert Agent started
[10:32:05] ELK found 427 errors
[10:32:06] K8s found pod restarts
[10:32:07] Metrics found latency spike
[10:32:09] RCA analysis started
```

------------------------------------------------------------------------

# PR 032 --- End-to-End Golden Scenario

## Goal

One command should reproduce a complete incident.

``` bash
make demo
```

It should:

1.  Start infrastructure.
2.  Start sample services.
3.  Inject an incident.
4.  Generate logs.
5.  Generate metrics.
6.  Trigger an alert.
7.  Create a deployment change.
8.  Submit incident.
9.  Run investigation.
10. Produce RCA.
11. Display result.

------------------------------------------------------------------------

# PR 033 --- Fault Injection Framework

## Goal

Make incident testing deterministic.

Create:

``` text
faults/
├── db-timeout
├── high-latency
├── memory-leak
├── crash-loop
├── network-error
├── bad-config
└── dependency-failure
```

Example:

``` bash
make inject-fault TYPE=db-timeout SERVICE=payment-service
```

------------------------------------------------------------------------

# PR 034 --- Security Boundary

## Goal

Harden the system before adding remediation.

Implement:

``` text
authentication
authorization
tenant isolation
secret management
MCP permission boundaries
audit logs
tool allowlists
rate limits
query limits
```

Every tool invocation should have:

``` text
user
tenant
agent
tool
timestamp
arguments
result status
```

Never log credentials.

------------------------------------------------------------------------

# PR 035 --- Agent Guardrails

Every agent should have:

``` text
max_steps
max_tool_calls
max_execution_time
allowed_tools
allowed_resources
confidence threshold
```

Example:

``` yaml
elk_agent:
  allowed_tools:
    - search_logs
    - get_mapping
    - list_indices

  max_tool_calls: 10
  max_execution_seconds: 60
```

------------------------------------------------------------------------

# PR 036 --- Human Approval Framework

## Goal

Separate investigation from remediation.

Safe:

``` text
read logs
read metrics
read Kubernetes state
read Git
read Jira
```

Requires approval:

``` text
restart deployment
scale deployment
rollback
modify config
delete pod
create Jira ticket
```

Architecture:

``` text
Agent
 ↓
Proposed Action
 ↓
Policy Engine
 ↓
Human Approval
 ↓
Execution
```

------------------------------------------------------------------------

# PR 037 --- Remediation Agent v1

Start with one safe action.

Example:

``` text
Restart payment-service
```

But the agent does NOT execute automatically.

It creates:

``` json
{
  "action": "restart_deployment",
  "resource": "payment-service",
  "reason": "...",
  "risk": "medium",
  "requires_approval": true
}
```

------------------------------------------------------------------------

# PR 038 --- A2A Communication Layer

Only introduce A2A after the individual agents work reliably.

Define agent contracts:

``` text
Orchestrator Agent
ELK Agent
K8s Agent
Metrics Agent
Code Agent
RCA Agent
Remediation Agent
```

Each agent receives structured tasks and returns structured results.

Do not use free-form text as the primary inter-agent protocol.

------------------------------------------------------------------------

# PR 039 --- Agent Registry

Create:

``` text
Agent Registry
```

Example:

``` json
{
  "name": "elk-agent",
  "version": "1.0",
  "capabilities": [
    "log-search",
    "error-analysis",
    "trace-correlation"
  ],
  "tools": [
    "search_logs",
    "get_mapping"
  ]
}
```

The orchestrator can discover capabilities dynamically.

------------------------------------------------------------------------

# PR 040 --- Reliability Engineering

Add:

``` text
timeouts
retries
circuit breakers
idempotency
dead-letter handling
partial failure handling
agent cancellation
```

Example:

``` text
ELK unavailable
      ↓
K8s + Metrics still execute
      ↓
Investigation becomes PARTIAL
      ↓
Response clearly states missing evidence
```

Never fail the entire investigation because one data source is
unavailable.

------------------------------------------------------------------------

# PR 041 --- Observability of the AI Platform

Monitor:

``` text
agent latency
LLM latency
token usage
tool latency
tool errors
MCP errors
investigation duration
cost per investigation
RCA confidence
human override rate
```

Create dashboards.

------------------------------------------------------------------------

# PR 042 --- Evaluation Framework

Create an evaluation dataset.

For each scenario:

``` text
Input incident
Expected service
Expected evidence
Expected root cause
Expected supporting signals
Expected irrelevant signals
```

Metrics:

``` text
Service identification accuracy
Evidence retrieval accuracy
RCA accuracy
False positive rate
Tool-call efficiency
Latency
Cost
```

Do not optimize solely for LLM confidence.

------------------------------------------------------------------------

# PR 043 --- Prompt Versioning

Store prompts as versioned artifacts.

``` text
prompts/
├── orchestrator/
│   ├── v1.md
│   └── v2.md
├── elk/
│   └── v1.md
├── rca/
│   └── v1.md
└── response/
    └── v1.md
```

Every investigation records:

``` text
model
prompt version
agent version
tool versions
```

This makes debugging reproducible.

------------------------------------------------------------------------

# PR 044 --- Cost Controls

Add:

``` text
max tokens
model routing
query caching
result caching
agent budgets
per-tenant limits
```

Use smaller models for:

``` text
parameter extraction
classification
simple summarization
```

Use stronger reasoning models for:

``` text
cross-agent correlation
RCA
complex investigations
```

------------------------------------------------------------------------

# PR 045 --- Multi-Tenant SaaS Foundation

Introduce:

``` text
Organization
User
Role
Project
Environment
Service
Incident
Investigation
```

Every database query must enforce tenant scope.

------------------------------------------------------------------------

# PR 046 --- RBAC

Roles:

``` text
Viewer
Engineer
Incident Commander
Admin
Platform Admin
```

Example:

``` text
Viewer
  → read incidents

Engineer
  → investigate

Incident Commander
  → approve remediation

Admin
  → configure integrations
```

------------------------------------------------------------------------

# PR 047 --- Integration Management

Build UI:

``` text
Settings
 └── Integrations
      ├── Elasticsearch
      ├── Kubernetes
      ├── Prometheus
      ├── GitHub
      ├── Jira
      └── Slack
```

Never expose raw secrets after saving.

------------------------------------------------------------------------

# PR 048 --- Production Docker Images

Create minimal images for:

``` text
api
orchestrator
elk-agent
k8s-agent
metrics-agent
code-agent
rca-agent
web
```

Use:

``` text
multi-stage builds
non-root users
health checks
read-only filesystem where possible
minimal base images
```

------------------------------------------------------------------------

# PR 049 --- Production Kubernetes Deployment

Create Helm charts:

``` text
helm/
└── ai-sre-platform/
    ├── Chart.yaml
    ├── values.yaml
    └── templates/
```

Deploy:

``` text
web
api
orchestrator
agents
workers
postgres
redis
```

Externalize:

``` text
secrets
configuration
integration credentials
```

------------------------------------------------------------------------

# PR 050 --- Production Readiness

Final checklist:

``` text
[ ] Security review
[ ] Threat model
[ ] RBAC
[ ] Tenant isolation
[ ] Secret management
[ ] Audit logging
[ ] Rate limiting
[ ] Backup strategy
[ ] Disaster recovery
[ ] Observability
[ ] Alerting
[ ] Cost controls
[ ] Agent evaluation
[ ] Prompt versioning
[ ] MCP security
[ ] Human approval
[ ] Failure handling
[ ] Load testing
[ ] Documentation
```

------------------------------------------------------------------------

# 8. Recommended GitHub Issue Structure

Create one GitHub Epic for each major area.

``` text
EPIC-001 Platform Foundation
EPIC-002 MCP Infrastructure
EPIC-003 ELK Agent
EPIC-004 Kubernetes Agent
EPIC-005 Metrics Agent
EPIC-006 Alert Agent
EPIC-007 Code Agent
EPIC-008 Knowledge Agent
EPIC-009 Orchestrator
EPIC-010 RCA
EPIC-011 Web UI
EPIC-012 Security
EPIC-013 Evaluation
EPIC-014 Production
```

Each PR should close one or more issues.

Example:

``` text
feat(elk): implement log investigation agent

Closes #42
Closes #43
```

------------------------------------------------------------------------

# 9. GitHub Labels

Recommended labels:

``` text
agent
mcp
backend
frontend
infra
security
observability
testing
evaluation
documentation
bug
feature
refactor
performance
good-first-issue
blocked
architecture
```

------------------------------------------------------------------------

# 10. Pull Request Template

``` markdown
## Summary

<!-- What does this PR change? -->

## Related Issue

Closes #

## Architecture Impact

<!-- Does this change an agent, MCP server, API, database, or infrastructure? -->

## Changes

- [ ]
- [ ]
- [ ]

## Testing

- [ ] Unit tests
- [ ] Integration tests
- [ ] E2E tests
- [ ] Manual testing

## Security

- [ ] No secrets committed
- [ ] Permissions reviewed
- [ ] Tool access scoped
- [ ] Input validated

## Observability

- [ ] Logs added
- [ ] Metrics added
- [ ] Errors handled

## Documentation

- [ ] README updated
- [ ] Architecture docs updated
- [ ] API/schema docs updated

## Screenshots

<!-- UI changes only -->

## Risk

Low / Medium / High

## Rollback

<!-- How can this change be reverted safely? -->
```

------------------------------------------------------------------------

# 11. Local Developer Experience

The target should eventually be:

``` bash
git clone <repository>

cd ai-sre-platform

cp .env.example .env

make setup

make infra-up

make seed

make services-up

make demo
```

Then open:

``` text
http://localhost:3000
```

The developer should see:

``` text
AI SRE
  ↓
Create Investigation
  ↓
"Payment API is failing"
  ↓
Investigate
```

------------------------------------------------------------------------

# 12. Local Infrastructure

Target local topology:

``` text
                         Docker Host
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  AI Platform                                                 │
│  ┌──────────┐ ┌─────────────┐ ┌──────────────┐             │
│  │ Web      │ │ API         │ │ Orchestrator │             │
│  └──────────┘ └─────────────┘ └──────┬───────┘             │
│                                      │                       │
│       ┌──────────────────────────────┼────────────────┐      │
│       ▼              ▼               ▼                ▼      │
│     ELK Agent     K8s Agent     Metrics Agent     Code Agent│
│       │              │               │                │      │
│       ▼              ▼               ▼                ▼      │
│     MCP            MCP             MCP              MCP      │
│       │              │               │                │      │
│       ▼              ▼               ▼                ▼      │
│ Elasticsearch     Kind/K3d       Prometheus         Git      │
│ Kibana                           Grafana            GitHub   │
│                                                              │
│  PostgreSQL                     Redis                        │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

------------------------------------------------------------------------

# 13. Example Complete Investigation

## Input

``` text
Payment API is returning HTTP 500 in production.
```

## Orchestrator

Extracts:

``` text
service = payment-service
environment = production
symptom = HTTP 500
time_range = 1 hour
```

## ELK Agent

Finds:

``` text
427 HTTP 500 errors

Top exception:
Database connection timeout
```

## Metrics Agent

Finds:

``` text
5xx:
2% → 25%

p95 latency:
180ms → 5.2s
```

## Kubernetes Agent

Finds:

``` text
payment-service pod restarts: 17

Reason:
CrashLoopBackOff
```

## Alert Agent

Finds:

``` text
DatabaseConnectionPoolExhausted
started at 10:29
```

## Code Agent

Finds:

``` text
Deployment:
v1.8.2

Commit:
abc123

Changed:
database connection pool configuration
```

## RCA Agent

Produces:

``` text
Root Cause Hypothesis

A database connection-pool configuration change
introduced in v1.8.2 is strongly correlated with
connection exhaustion and subsequent API failures.

Confidence: 0.91
```

## Response

``` text
Incident: INC-1024

Impact:
Payment API returning HTTP 500.

Evidence:
- 427 HTTP 500 errors
- Database timeout exceptions
- p95 latency increased to 5.2s
- 17 pod restarts
- Database pool alert triggered
- Deployment v1.8.2 introduced immediately before incident

Recommended next steps:
1. Compare pool configuration with v1.8.1.
2. Validate DB connection limits.
3. Review deployment configuration.
4. Consider rollback after human approval.
```

------------------------------------------------------------------------

# 14. What NOT to Build Initially

Do not start with:

``` text
20 autonomous agents
```

Do not start with:

``` text
automatic production remediation
```

Do not start with:

``` text
fully autonomous Kubernetes changes
```

Do not start with:

``` text
complex A2A network
```

Do not make the LLM directly execute arbitrary:

``` text
kubectl
curl
SQL
Elasticsearch
Git
```

commands.

The first milestone should be:

``` text
Incident
   ↓
Orchestrator
   ↓
ELK Agent
   ↓
Elasticsearch MCP
   ↓
Evidence
   ↓
Useful answer
```

Then expand.

------------------------------------------------------------------------

# 15. Definition of Done for the MVP

The MVP is complete when:

``` text
[ ] User can create an incident.
[ ] Orchestrator understands the incident.
[ ] Orchestrator identifies service/environment/time range.
[ ] ELK Agent can investigate logs.
[ ] Elasticsearch is accessed through MCP.
[ ] Kubernetes Agent can inspect runtime state.
[ ] Metrics Agent can inspect Prometheus.
[ ] Code Agent can inspect Git history.
[ ] Multiple agents can execute concurrently.
[ ] Evidence is persisted.
[ ] RCA Agent correlates evidence.
[ ] Final response cites evidence.
[ ] UI displays investigation progress.
[ ] Fault-injection scenarios are reproducible.
[ ] Unit tests exist.
[ ] Integration tests exist.
[ ] E2E golden scenario passes.
[ ] CI is enforced through GitHub Actions.
[ ] No production write action occurs without approval.
```

------------------------------------------------------------------------

# 16. Final Target Architecture

``` text
                          ┌─────────────────────┐
                          │      Engineer       │
                          └──────────┬──────────┘
                                     │
                                     ▼
                          ┌─────────────────────┐
                          │     Web / API       │
                          └──────────┬──────────┘
                                     │
                                     ▼
                    ┌────────────────────────────────┐
                    │       ORCHESTRATOR AGENT       │
                    │                                │
                    │ Intent → Plan → Delegate       │
                    │ Correlate → Validate → Report  │
                    └───────────────┬────────────────┘
                                    │
          ┌─────────────────────────┼─────────────────────────┐
          │                         │                         │
          ▼                         ▼                         ▼
     ELK Agent                 K8s Agent                 Code Agent
          │                         │                         │
       MCP                         MCP                      MCP
          │                         │                         │
          ▼                         ▼                         ▼
   Elasticsearch               Kubernetes                   Git
     + Kibana
          │                         │                         │
          └─────────────────────────┼─────────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
              ▼                     ▼                     ▼
        Metrics Agent          Alert Agent          Knowledge Agent
              │                     │                     │
             MCP                   MCP                   MCP
              │                     │                     │
         Prometheus            Alertmanager            Runbooks
              │                     │                     │
              └─────────────────────┼─────────────────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │   Evidence Store     │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │      RCA Agent       │
                         │                      │
                         │ Facts                │
                         │ Correlations         │
                         │ Hypotheses           │
                         │ Confidence            │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │  Response Builder    │
                         └──────────┬───────────┘
                                    │
                                    ▼
                   ┌─────────────────────────────────┐
                   │ Incident Report                 │
                   │                                 │
                   │ Timeline                        │
                   │ Findings                        │
                   │ Evidence                        │
                   │ RCA hypothesis                  │
                   │ Recommended actions             │
                   │ Kibana/Grafana/Git links        │
                   └─────────────────────────────────┘
```

------------------------------------------------------------------------

# 17. Engineering Sequence Summary

The entire project should progress in this order:

``` text
01  Repository
02  CI/CD
03  Configuration
04  Domain schemas
05  MCP framework
06  Elasticsearch
07  Elasticsearch MCP
08  ELK Agent
09  ELK investigation loop
10  ELK evaluation
11  Kubernetes
12  Kubernetes MCP
13  K8s Agent
14  Prometheus/Grafana
15  Metrics MCP
16  Metrics Agent
17  Alertmanager
18  Alert Agent
19  Git repository
20  Git MCP
21  Code Agent
22  Knowledge Agent
23  Jira Agent
24  Orchestrator
25  Parallel execution
26  Evidence store
27  RCA Agent
28  Response Builder
29  Timeline
30  Web UI
31  Streaming
32  E2E golden scenario
33  Fault injection
34  Security
35  Guardrails
36  Human approval
37  Remediation
38  A2A
39  Agent registry
40  Reliability
41  AI observability
42  Evaluation
43  Prompt versioning
44  Cost controls
45  Multi-tenancy
46  RBAC
47  Integrations
48  Production containers
49  Production Kubernetes
50  Production readiness
```

This sequence deliberately builds **one reliable capability at a time**,
while every PR leaves the repository in a runnable state.
