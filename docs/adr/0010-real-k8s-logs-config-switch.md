# ADR-0010: Real Kubernetes logs via Fluent Bit, switched by configuration

- **Status:** Accepted
- **Date:** 2026-09-26

## Context
MASTER_PLAN D12: the Log agent started on synthetic logs (one index family per service,
`payment-prod-*`, ...) and must move to the real logs of the sample services in Minikube
**without agent code changes**, as an early portability proof (PR-016).

Real Kubernetes logs differ in two ways that matter to the agent:
1. **One shared index for every workload.** A log shipper writes all pods to one index
   family; the service is a field of each line (the app's `service`, or the pod's labels).
2. **Metadata lives under `kubernetes.*`.** The app's own JSON fields must be at the top
   level so the same field mapping works.

Also: `logs-k8s-*` matches Elasticsearch's built-in `logs-*-*` data-stream template, and ES
runs with a 512 MB heap inside a 5 GB Docker budget.

## Decision
- **Fluent Bit** (`fluent/fluent-bit:5.1.2`, pinned) as a DaemonSet in namespace `logging`
  (64 Mi limit): tail `*_prod_*.log` (CRI parser) → `kubernetes` filter (labels, no
  annotations) → `parser` filter lifting the app JSON to the top level (the app's
  `@timestamp` becomes the event time) → `es` output, `Logstash_Format` prefix `logs-k8s`.
  Only namespace `prod` is shipped.
- **Our own index template** (`aiops-k8s-logs`, priority 200 > the built-in 100) with the
  synthetic field mappings plus `kubernetes.*`, and an **ILM policy deleting indices after
  2 days**. Installed idempotently by `aiops seed k8s-logging` / `make logging-up`.
- **Config switch = a new environment** `local-k8s`, not an edit of `local.yaml`, so the
  synthetic fixtures and evals keep working side by side. A generic **`extends:`** in the
  config loader (environments: deep merge; service catalogs: services merged by `name`)
  keeps `local-k8s` to the few lines that differ and inherits everything else.
- **Shared index → one generic, opt-in agent setting**: `logs.settings.service_filter: true`
  prefixes every query with `WHERE <fields.service> == "<catalog logs.service_value>"`
  (and adds the clause to UI links and the prompt). Off by default: synthetic queries are
  byte-for-byte unchanged. In `local-k8s`, `fields.service` is mapped to the Kubernetes
  label `kubernetes.labels.app`.

## Alternatives considered
- **Filtered index aliases per service** (true zero code change): couples the ES template
  to the service catalog, needs an alias per service, and wildcard queries by the LLM would
  still see every service. Shared indices + a service field is how most companies store
  logs, so the knob is the more portable answer.
- **Editing `local.yaml` in place** (as the plan said): breaks the synthetic fixtures/evals
  that other agents rely on, and loses the side-by-side comparison.
- **Copying `local.yaml`**: two ~200-line files drifting apart while other PRs keep adding
  capabilities to `local.yaml`. `extends:` avoids it.
- **Merge_Log in the kubernetes filter**: lifts the JSON too, but keeps the app's
  `@timestamp` as a string next to the one the `es` output writes (duplicate key); the
  parser filter with `Time_Key @timestamp` avoids that.
- **Non-root Fluent Bit**: kubelet's log files are `root:root 0640` in `0750` dirs; a
  non-root UID can't read them and Kubernetes grants no file capabilities to non-root
  processes. We run UID 0 with every capability dropped, a read-only root filesystem and
  `/var/log` mounted read-only.

## Consequences
- `AIOPS_ENV=local-k8s aiops agent run logs ...` works on real logs; `local` is unchanged.
- The Log agent (and later agents) can be pointed at any company's shared log index with
  catalog + field mapping + `service_filter`, no code.
- Fluent Bit costs ~9 MB on the node (measured); `logs-k8s-*` adds ~17 MB/hour to ES and
  expires after 2 days.
- Live fixtures are not at a fixed time: each recorded window is stored in `meta.json`.
