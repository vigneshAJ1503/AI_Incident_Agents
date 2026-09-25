# Knowledge base

Runbooks and service docs that the Knowledge agent searches (MASTER_PLAN.md D5, UC-09). The markdown in git is the source of truth. `make ingest-knowledge` indexes it into Postgres full-text search (schema `knowledge`), and `knowledge-mcp` serves it read-only.

```
knowledge-base/
├── runbooks/   # one incident type per file: symptoms, diagnosis, mitigation, rollback, escalation
└── services/   # one file per service: owners, dependencies, endpoints, dashboards, SLOs
```

## Front matter
Every file starts with YAML front matter. The ingester uses it for filtering and ranking:

```yaml
---
title: Database connection pool exhaustion   # defaults to the first H1
type: runbook                               # runbook | service | doc
services: [payment-service, order-service]  # [] = applies to every service
tags: [database, connection-pool]
alerts: [DatabaseConnectionPoolExhausted]   # alert names that link here (runbook_url)
owner: platform-sre
last_reviewed: 2026-09-01
---
```

## Writing a runbook
- Use the section names `Summary`, `Symptoms`, `Known issues`, `Diagnosis`, `Mitigation`, `Rollback`, `Escalation`. The Knowledge agent extracts these by name.
- Quote log messages and alert names **exactly** (for example `could not acquire a connection from the pool`). Search matches on words, and the agent matches log patterns against `Symptoms` and `Known issues`.
- Keep each section self-contained: search results are sections, shown with their heading path (for example "Database connection pool exhaustion > Diagnosis > 1. Confirm the pool is the bottleneck").
- File names are stable identifiers. Alert `runbook_url`s and the service catalog (`config/service-catalog/*.yaml`, `runbooks:`) refer to them.

After editing, run `make ingest-knowledge`. Ingestion is idempotent: unchanged files are skipped, changed files are re-chunked, and deleted files are removed from the index.
