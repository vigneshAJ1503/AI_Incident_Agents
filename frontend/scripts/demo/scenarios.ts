/**
 * Scenario ground truth for the demo dataset. Numbers come from the recorded replay fixtures
 * (backend/tests/fixtures/<agent>/S1..S5) and the scenario READMEs; clock times are moved onto the
 * demo calendar (the week ending 2026-09-28).
 */
import type { Evidence, EvidenceKind } from "../../src/lib/api/schemas";
import * as L from "./links";
import { MIN, SEC, at, iso, mulberry32, ramp, series } from "./rng";
import type { ScenarioSpec } from "./types";

const rand = mulberry32(20260928);

function ev(
  id: string,
  kind: EvidenceKind,
  source: string,
  summary: string,
  opts: {
    ts?: number;
    link?: string | null;
    query?: string | null;
    data?: Record<string, unknown>;
  } = {},
): Evidence {
  return {
    id,
    kind,
    source,
    summary,
    timestamp: opts.ts === undefined ? null : iso(opts.ts),
    link: opts.link ?? null,
    query: opts.query ?? null,
    data: opts.data ?? {},
  };
}

function metric(
  id: string,
  source: string,
  summary: string,
  o: {
    metric: string;
    unit: "percent" | "seconds" | "ratio" | "rps" | "bytes" | "count" | "bool";
    service: string;
    start: number;
    end: number;
    anomalyStart: number | null;
    fn: (t: number) => number;
    baseline: number;
    peak: number;
    query: string;
    panel: string;
    noise?: number;
    extra?: { label: string; fn: (t: number) => number }[];
  },
): Evidence {
  const main = {
    label: o.service,
    points: series(rand, o.start, o.end, 30, o.fn, o.noise ?? 0.08),
  };
  const extra = (o.extra ?? []).map((x) => ({
    label: x.label,
    points: series(rand, o.start, o.end, 30, x.fn, o.noise ?? 0.08),
  }));
  return ev(id, "metric", "prometheus", summary, {
    ts: o.anomalyStart ?? o.end,
    query: o.query,
    link: L.grafana(o.service, o.panel, o.start, o.end),
    data: {
      metric: o.metric,
      unit: o.unit,
      baseline: o.baseline,
      peak: o.peak,
      anomaly: o.anomalyStart === null ? null : { start: iso(o.anomalyStart), end: iso(o.end) },
      series: [main, ...extra],
    },
  });
}

function logPattern(
  id: string,
  summary: string,
  o: {
    index: string;
    pattern: string;
    level: "ERROR" | "WARN" | "INFO";
    count: number;
    baseline: number;
    first: number;
    last: number;
    samples: string[];
    service: string;
    start: number;
    end: number;
    query: string;
    extra?: Record<string, unknown>;
  },
): Evidence {
  return ev(id, "log", "elasticsearch", summary, {
    ts: o.first,
    query: o.query,
    link: L.kibana(o.index, o.query, o.start, o.end),
    data: {
      pattern: o.pattern,
      level: o.level,
      count: o.count,
      baseline_count: o.baseline,
      first_seen: iso(o.first),
      last_seen: iso(o.last),
      service: o.service,
      samples: o.samples,
      ...(o.extra ?? {}),
    },
  });
}

// ---------------------------------------------------------------------------------------------
// S1: DB connection pool misconfiguration (payment-service v1.8.2)
// ---------------------------------------------------------------------------------------------
function s1(): ScenarioSpec {
  const created = at("2026-09-28T10:30:00Z");
  const ws = at("2026-09-28T10:00:00Z");
  const we = created;
  const rollout = at("2026-09-28T10:08:00Z");
  const firstTimeout = at("2026-09-28T10:10:07Z");
  const esql =
    'FROM payment-prod-* | WHERE level IN ("ERROR","WARN") | STATS count = COUNT(*) BY pattern';
  const logLine = (ts: string, lvl: string, msg: string, trace: string) =>
    `${ts} ${lvl} [payment-service v1.8.2] trace=${trace} ${msg}`;
  const evidence = {
    logs: [
      logPattern(
        "ev-s1-logs-1",
        "New ERROR pattern 'Database connection timeout' on POST /api/v1/pay: 44 events since 10:10Z (0 in the 24 h baseline)",
        {
          index: "payment-prod-*",
          pattern:
            "Database connection timeout: could not acquire a connection from the pool within <NUM>ms (pool size=<NUM>, active=<NUM>, waiting=<NUM>)",
          level: "ERROR",
          count: 44,
          baseline: 0,
          first: firstTimeout,
          last: at("2026-09-28T10:29:43Z"),
          service: "payment-service",
          start: ws,
          end: we,
          query: esql,
          samples: [
            logLine(
              "2026-09-28T10:10:07.571Z",
              "ERROR",
              "Database connection timeout: could not acquire a connection from the pool within 5000ms (pool size=2, active=2, waiting=10)",
              "96a931cfdc9709fe",
            ),
            logLine(
              "2026-09-28T10:17:34.843Z",
              "ERROR",
              "Database connection timeout: could not acquire a connection from the pool within 5000ms (pool size=2, active=2, waiting=27)",
              "cd3ae34c1599143f",
            ),
            logLine(
              "2026-09-28T10:16:29.126Z",
              "ERROR",
              "Database connection timeout: could not acquire a connection from the pool within 5000ms (pool size=2, active=2, waiting=39)",
              "5118606ede41fb20",
            ),
          ],
        },
      ),
      logPattern(
        "ev-s1-logs-2",
        "WARN 'HikariPool-1 - Connection is not available, request timed out after 5000ms': 18 events since 10:12Z",
        {
          index: "payment-prod-*",
          pattern:
            "HikariPool-<NUM> - Connection is not available, request timed out after <NUM>ms",
          level: "WARN",
          count: 18,
          baseline: 0,
          first: at("2026-09-28T10:12:31Z"),
          last: at("2026-09-28T10:29:43Z"),
          service: "payment-service",
          start: ws,
          end: we,
          query: esql,
          samples: [
            logLine(
              "2026-09-28T10:12:31.832Z",
              "WARN",
              "HikariPool-1 - Connection is not available, request timed out after 5000ms",
              "e42bdc7dbc8c8d08",
            ),
          ],
        },
      ),
      logPattern(
        "ev-s1-logs-3",
        "All timeout errors come from pods running v1.8.2 (302 log lines since 10:08Z, 3 pod starts); v1.8.1 pods logged none",
        {
          index: "payment-prod-*",
          pattern: "version breakdown",
          level: "INFO",
          count: 302,
          baseline: 0,
          first: rollout,
          last: we,
          service: "payment-service",
          start: ws,
          end: we,
          query: "FROM payment-prod-* | STATS count = COUNT(*), starts = SUM(is_start) BY version",
          samples: [
            "v1.8.2: 302 lines, 3 starts, 62 errors",
            "v1.8.1: 87 lines, 0 errors (drained at 10:08Z)",
          ],
          extra: { versions: { "v1.8.2": 302, "v1.8.1": 87 } },
        },
      ),
    ],
    metrics: [
      metric(
        "ev-s1-metrics-1",
        "5xx ratio",
        "HTTP 5xx ratio rose from 0.5% to 24.6% at 10:10Z (≈49×)",
        {
          metric: "http_5xx_ratio",
          unit: "percent",
          service: "payment-service",
          start: ws,
          end: we,
          anomalyStart: firstTimeout,
          fn: (t) => 0.5 + 24.1 * ramp(t, firstTimeout - 30 * SEC, firstTimeout + 3 * MIN),
          baseline: 0.5,
          peak: 24.6,
          panel: "Error rate",
          query:
            'sum(rate(http_requests_total{service="payment-service",status=~"5.."}[2m])) / sum(rate(http_requests_total{service="payment-service"}[2m]))',
          noise: 0.12,
        },
      ),
      metric(
        "ev-s1-metrics-2",
        "p95 latency",
        "p95 latency on payment-service went from 0.21 s to 9.48 s (45×), pinned near the 5 s pool timeout ×2 retries",
        {
          metric: "http_request_duration_p95",
          unit: "seconds",
          service: "payment-service",
          start: ws,
          end: we,
          anomalyStart: at("2026-09-28T10:09:30Z"),
          fn: (t) => 0.21 + 9.27 * ramp(t, at("2026-09-28T10:09:00Z"), at("2026-09-28T10:12:00Z")),
          baseline: 0.21,
          peak: 9.48,
          panel: "Latency p95",
          query:
            'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="payment-service"}[2m])))',
          extra: [
            {
              label: "p99",
              fn: (t) =>
                0.34 + 9.56 * ramp(t, at("2026-09-28T10:09:00Z"), at("2026-09-28T10:12:00Z")),
            },
          ],
        },
      ),
      metric(
        "ev-s1-metrics-3",
        "DB pool saturation",
        "DB connection pool saturated at 100% (2/2 active) from 10:09Z; up to 18 requests waiting",
        {
          metric: "db_pool_saturation",
          unit: "percent",
          service: "payment-service",
          start: ws,
          end: we,
          anomalyStart: at("2026-09-28T10:09:00Z"),
          fn: (t) =>
            t < rollout ? 8 : 100 * Math.max(0.3, ramp(t, rollout, at("2026-09-28T10:09:00Z"))),
          baseline: 8,
          peak: 100,
          panel: "DB pool",
          query:
            'sum(db_pool_connections_active{service="payment-service"}) / sum(db_pool_connections_max{service="payment-service"})',
          noise: 0.02,
          extra: [
            {
              label: "waiting",
              fn: (t) =>
                t < at("2026-09-28T10:09:00Z") ? 0 : 4 + 14 * Math.abs(Math.sin(t / 97000)),
            },
          ],
        },
      ),
      metric(
        "ev-s1-metrics-4",
        "Throughput",
        "Request rate stayed flat at ≈3 req/s: no traffic spike",
        {
          metric: "http_requests_rate",
          unit: "rps",
          service: "payment-service",
          start: ws,
          end: we,
          anomalyStart: null,
          fn: () => 3,
          baseline: 3,
          peak: 3.3,
          panel: "Throughput",
          query: 'sum(rate(http_requests_total{service="payment-service"}[2m]))',
          noise: 0.07,
        },
      ),
    ],
    alerts: [
      ev(
        "ev-s1-alerts-1",
        "alert",
        "alertmanager",
        "DatabaseConnectionPoolExhausted (critical) firing since 10:11Z: pool ≥ 95% used, requests waiting",
        {
          ts: at("2026-09-28T10:11:00Z"),
          link: L.alertmanager("DatabaseConnectionPoolExhausted"),
          data: {
            alertname: "DatabaseConnectionPoolExhausted",
            severity: "critical",
            state: "active",
            starts_at: "2026-09-28T10:11:00Z",
            runbook_url: "knowledge-base/runbooks/database-connection-pool.md",
            labels: { service: "payment-service", namespace: "prod", team: "payments" },
          },
        },
      ),
      ev(
        "ev-s1-alerts-2",
        "alert",
        "alertmanager",
        "HighErrorRate (critical) firing since 10:12Z: 24.6% of requests return 5xx (threshold 5%)",
        {
          ts: at("2026-09-28T10:12:00Z"),
          link: L.alertmanager("HighErrorRate"),
          data: {
            alertname: "HighErrorRate",
            severity: "critical",
            state: "active",
            starts_at: "2026-09-28T10:12:00Z",
            runbook_url: "knowledge-base/runbooks/high-error-rate.md",
            labels: { service: "payment-service", namespace: "prod", team: "payments" },
          },
        },
      ),
    ],
    k8s: [
      ev(
        "ev-s1-k8s-1",
        "k8s_event",
        "kubernetes",
        "Deployment payment-service rolled out revision 14 at 10:08Z, change-cause 'v1.8.2: tune db pool (DB_POOL_SIZE 20 -> 2)'",
        {
          ts: rollout,
          link: L.grafanaK8s("payment-service", ws, we),
          data: {
            reason: "RolloutRevision",
            object: "deployment/payment-service",
            namespace: "prod",
            revision: "14",
            change_cause: "v1.8.2: tune db pool (DB_POOL_SIZE 20 -> 2)",
            image: "aiops/sample-service:0.1.0",
            previous: { revision: "13", change_cause: "v1.8.1" },
          },
        },
      ),
      ev(
        "ev-s1-k8s-2",
        "k8s_event",
        "kubernetes",
        "ScalingReplicaSet: scaled up payment-service-76869cbcc9 to 1, old ReplicaSet to 0",
        {
          ts: rollout + 6 * SEC,
          data: {
            reason: "ScalingReplicaSet",
            object: "deployment/payment-service",
            type: "Normal",
            count: 2,
          },
        },
      ),
      ev(
        "ev-s1-k8s-3",
        "k8s_event",
        "kubernetes",
        "Pods are healthy: 1/1 ready, 0 restarts, no OOMKilled or probe failures",
        {
          ts: we,
          data: {
            reason: "PodHealth",
            object: "pod/payment-service-76869cbcc9-x2l8d",
            ready: "1/1",
            restarts: 0,
          },
        },
      ),
      ev(
        "ev-s1-k8s-4",
        "k8s_event",
        "kubernetes",
        "Memory 62 Mi of 160 Mi limit; CPU 9 m: resources are not the bottleneck",
        {
          ts: we,
          data: {
            reason: "ResourceUsage",
            object: "pod/payment-service-76869cbcc9-x2l8d",
            memory: "62Mi/160Mi",
            cpu: "9m",
          },
        },
      ),
    ],
    code: [
      ev(
        "ev-s1-code-1",
        "commit",
        "git",
        'Commit 739d116 \'tune db pool\' by Jordan Lee changed DB_POOL_SIZE from "20" to "2"',
        {
          ts: at("2026-09-28T08:20:00Z"),
          data: {
            sha: "739d116b00183aa75ec567bab5d540734a77c2ed",
            short_sha: "739d116b00",
            author: "Jordan Lee",
            subject: "tune db pool",
            risky: true,
            files: [
              {
                path: "services/payment-service/config/app.yaml",
                status: "modified",
                hunk: '@@ -2,7 +2,7 @@\n service: payment-service\n env:\n   DB_HOST: "postgres.prod.svc"\n-  DB_POOL_SIZE: "20"\n+  DB_POOL_SIZE: "2"\n   DB_TIMEOUT_MS: "5000"\n   REDIS_URL: "redis://redis.prod.svc:6379/0"\n   ISSUER_TIMEOUT_MS: "3000"',
              },
            ],
          },
        },
      ),
      ev(
        "ev-s1-code-2",
        "commit",
        "git",
        "Release payment-service/v1.8.2 (09831d4 by Priya Raman) at 09:55Z ships the 'tune db pool' commit",
        {
          ts: at("2026-09-28T09:55:00Z"),
          data: {
            sha: "09831d4393c20bdbadecf7a93d5ede88f4402f59",
            short_sha: "09831d4393",
            author: "Priya Raman",
            subject: "release payment-service v1.8.2",
            tag: "payment-service/v1.8.2",
            files: [
              {
                path: "services/payment-service/app/__init__.py",
                status: "modified",
                hunk: '@@ -1 +1 @@\n-__version__ = "v1.8.1"\n+__version__ = "v1.8.2"',
              },
            ],
          },
        },
      ),
      ev(
        "ev-s1-code-3",
        "commit",
        "git",
        "Other changes in the window are low risk (receipt formatting helper, docs)",
        {
          ts: at("2026-09-27T20:30:00Z"),
          data: {
            sha: "4bccd960fbbe5d24a6312471a3dcb87183efefda",
            short_sha: "4bccd960fb",
            author: "Priya Raman",
            subject: "payment-service: extract receipt formatting helper",
            risky: false,
            files: [
              {
                path: "services/payment-service/app/receipts.py",
                status: "added",
                hunk: '@@ -0,0 +1,5 @@\n+"""Receipt formatting."""\n+\n+\n+def format_amount(amount_cents: int, currency: str) -> str:\n+    return f"{amount_cents / 100:.2f} {currency}"',
              },
            ],
          },
        },
      ),
    ],
    tickets: [
      ev(
        "ev-s1-tickets-1",
        "ticket",
        "jira",
        "Open known issue OPS-12 'payment-service DB connection timeouts' (High): same error, suspected pool sizing",
        {
          ts: at("2026-09-22T10:30:00Z"),
          link: L.ticket("OPS-12"),
          data: {
            key: "OPS-12",
            status: "Open",
            priority: "High",
            issue_type: "Bug",
            summary: "payment-service DB connection timeouts",
            labels: ["payment-service", "database", "known-issue"],
          },
        },
      ),
      ev(
        "ev-s1-tickets-2",
        "ticket",
        "jira",
        "Resolved OPS-2 (INC-2026-031): pool exhaustion after a release; fixed by rollback",
        {
          ts: at("2026-08-14T09:00:00Z"),
          link: L.ticket("OPS-2"),
          data: {
            key: "OPS-2",
            status: "Done",
            priority: "Highest",
            issue_type: "Incident",
            summary: "Payments failing: connection pool exhausted after release",
          },
        },
      ),
    ],
    knowledge: [
      ev(
        "ev-s1-knowledge-1",
        "doc",
        "knowledge-base",
        "Runbook 'Database connection pool exhaustion' matches both log patterns and both alerts; mitigation: roll back, then restore DB_POOL_SIZE=20",
        {
          link: L.runbook("database-connection-pool.md", "mitigation"),
          data: {
            path: "knowledge-base/runbooks/database-connection-pool.md",
            title: "Database connection pool exhaustion",
            section: "Mitigation",
            rank: 1,
            score: 0.92,
            excerpt:
              "1. If a release changed the pool settings, roll back first: kubectl rollout undo deployment/payment-service -n prod.\n2. If no release is involved, raise the pool size temporarily (ConfigMap DB_POOL_SIZE, then kubectl rollout restart).",
          },
        },
      ),
      ev(
        "ev-s1-knowledge-2",
        "doc",
        "knowledge-base",
        "Known issue INC-2026-031: v1.8.2 shipped DB_POOL_SIZE=2 in a commit titled 'tune db pool'",
        {
          link: L.runbook("database-connection-pool.md", "known-issues"),
          data: {
            path: "knowledge-base/runbooks/database-connection-pool.md",
            title: "Database connection pool exhaustion",
            section: "Known issues",
            rank: 1,
            score: 0.88,
            excerpt:
              'Pool size lowered by a release (INC-2026-031). payment-service v1.8.2 shipped DB_POOL_SIZE=2 (it was 20) in a commit titled "tune db pool". Fix: roll back to v1.8.1, then restore DB_POOL_SIZE=20 in the ConfigMap.',
          },
        },
      ),
      ev(
        "ev-s1-knowledge-3",
        "doc",
        "knowledge-base",
        "Runbook 'High error rate (HTTP 5xx)': triage steps for HighErrorRate",
        {
          link: L.runbook("high-error-rate.md"),
          data: {
            path: "knowledge-base/runbooks/high-error-rate.md",
            title: "High error rate (HTTP 5xx)",
            section: "Triage",
            rank: 2,
            score: 0.61,
            excerpt:
              "Check the first-seen time of new error patterns and compare it with the latest rollout.",
          },
        },
      ),
    ],
  };

  return {
    id: "S1",
    title: "DB connection pool misconfiguration (payment-service v1.8.2)",
    description:
      "v1.8.2 lowered DB_POOL_SIZE from 20 to 2; requests time out waiting for a connection (HTTP 500).",
    question: "Payment API is returning HTTP 500 in production",
    service: "payment-service",
    environment: "production",
    created,
    windowStart: ws,
    windowEnd: we,
    symptoms: ["db_timeout_errors_up", "error_rate_up", "latency_up"],
    rootCauseLabel: "DB pool misconfiguration",
    variants: [
      "Payment API is returning HTTP 500 in production",
      "Checkout payments failing with 500s",
      "POST /api/v1/pay errors spiking in prod",
      "Why are payments timing out?",
    ],
    agents: [
      {
        agent: "logs",
        objective:
          "Find new error patterns for payment-service in production over the last 30 minutes",
        start: 1400,
        duration: 6200,
        tools: [
          ["execute_esql", 420, { index: "payment-prod-*", query: "STATS count BY window, level" }],
          [
            "execute_esql",
            610,
            { index: "payment-prod-*", query: "STATS count, first_seen BY pattern" },
          ],
          ["execute_esql", 380, { index: "payment-prod-*", query: "STATS count BY version" }],
          [
            "execute_esql",
            290,
            { index: "payment-prod-*", query: "KEEP @timestamp, trace_id | LIMIT 20" },
          ],
        ],
        status: "success",
        summary:
          "A new ERROR pattern 'Database connection timeout: could not acquire a connection from the pool' appeared at 10:10Z (44 events, none in the baseline), plus 18 HikariPool timeouts. All of them come from v1.8.2 pods.",
        signals: [
          "db_timeout_errors_up",
          "error_rate_up",
          "new_error_pattern",
          "deployment_detected",
        ],
        evidence: evidence.logs,
        findings: [
          {
            type: "new_error_pattern",
            kind: "FACT",
            description:
              "44 'Database connection timeout' errors since 10:10Z; 0 in the 24 h baseline.",
            evidence_ids: ["ev-s1-logs-1"],
            confidence: 0.98,
          },
          {
            type: "pool_exhaustion",
            kind: "OBSERVATION",
            description:
              "Every timeout reports pool size=2 with active=2 and 7–39 requests waiting.",
            evidence_ids: ["ev-s1-logs-1", "ev-s1-logs-2"],
            confidence: 0.95,
          },
          {
            type: "version_correlation",
            kind: "CORRELATION",
            description: "Errors only come from v1.8.2 pods, which started at 10:08Z.",
            evidence_ids: ["ev-s1-logs-3"],
            confidence: 0.9,
          },
        ],
      },
      {
        agent: "metrics",
        objective:
          "Check error rate, latency and saturation for payment-service against the baseline",
        start: 1550,
        duration: 8400,
        tools: [
          ["query_range", 310, { query: "rate(http_requests_total[2m])" }],
          ["query_range", 290, { query: "5xx ratio" }],
          ["query_range", 350, { query: "histogram_quantile(0.95, …)" }],
          ["query_range", 330, { query: "db_pool_connections_active / db_pool_connections_max" }],
          ["query_range", 270, { query: "db_pool_connections_pending" }],
          ["query_range", 260, { query: "redis_up" }],
        ],
        status: "success",
        summary:
          "payment-service error rate up 0.5% → 24.6%, latency p95 up 0.21 s → 9.48 s, db pool saturated at 100% with up to 18 pending. Throughput is flat and Redis is up.",
        signals: ["error_rate_up", "db_pool_saturated", "latency_up"],
        evidence: evidence.metrics,
        findings: [
          {
            type: "error_rate_up",
            kind: "FACT",
            description: "5xx ratio 0.5% → 24.6% starting 10:10Z.",
            evidence_ids: ["ev-s1-metrics-1"],
            confidence: 0.97,
          },
          {
            type: "latency_up",
            kind: "FACT",
            description: "p95 latency 0.21 s → 9.48 s (45×) from 10:09Z.",
            evidence_ids: ["ev-s1-metrics-2"],
            confidence: 0.96,
          },
          {
            type: "db_pool_saturated",
            kind: "OBSERVATION",
            description:
              "DB pool at 100% (2/2) with up to 18 waiting, one minute after the rollout.",
            evidence_ids: ["ev-s1-metrics-3"],
            confidence: 0.95,
          },
          {
            type: "no_traffic_spike",
            kind: "OBSERVATION",
            description: "Request rate is flat at ≈3 req/s, so load did not change.",
            evidence_ids: ["ev-s1-metrics-4"],
            confidence: 0.9,
          },
        ],
      },
      {
        agent: "alerts",
        objective: "List firing and recently resolved alerts for payment-service",
        start: 1500,
        duration: 2300,
        tools: [
          [
            "list_alerts",
            180,
            { labels: { service: "payment-service", namespace: "prod" }, state: "all" },
          ],
          ["list_silences", 90, {}],
        ],
        status: "success",
        summary:
          "2 critical alerts firing: DatabaseConnectionPoolExhausted (since 10:11Z), then HighErrorRate (since 10:12Z). No silences.",
        signals: ["alerts_firing", "critical_alert_firing"],
        evidence: evidence.alerts,
        findings: [
          {
            type: "alerts_firing",
            kind: "FACT",
            description:
              "DatabaseConnectionPoolExhausted fired at 10:11Z, one minute before HighErrorRate.",
            evidence_ids: ["ev-s1-alerts-1", "ev-s1-alerts-2"],
            confidence: 0.99,
          },
        ],
      },
      {
        agent: "k8s",
        objective: "Check the payment-service deployment, pods and recent events in prod",
        start: 1650,
        duration: 4100,
        tools: [
          ["get_deployment", 240, { name: "payment-service", namespace: "prod", history: 5 }],
          ["list_pods", 210, { label_selector: "app=payment-service" }],
          ["list_events", 260, { namespace: "prod", kind: "Deployment" }],
          ["list_deployments", 180, { namespace: "prod" }],
        ],
        status: "success",
        summary:
          "Deployment payment-service rolled out revision 14 (v1.8.2: tune db pool) at 10:08Z. Pods are healthy: 1/1 ready, 0 restarts, memory well under the limit.",
        signals: ["recent_rollout", "healthy"],
        evidence: evidence.k8s,
        findings: [
          {
            type: "recent_rollout",
            kind: "FACT",
            description:
              "Revision 14 (v1.8.2, change-cause 'tune db pool') rolled out at 10:08Z, 2 minutes before the first error.",
            evidence_ids: ["ev-s1-k8s-1", "ev-s1-k8s-2"],
            confidence: 0.98,
          },
          {
            type: "healthy",
            kind: "OBSERVATION",
            description: "No restarts, OOMKills or probe failures; CPU and memory are normal.",
            evidence_ids: ["ev-s1-k8s-3", "ev-s1-k8s-4"],
            confidence: 0.95,
          },
        ],
      },
      {
        agent: "code",
        objective: "Find code and config changes to payment-service before the incident",
        start: 1700,
        duration: 5200,
        tools: [
          ["list_releases", 160, { repo: "sample-repo" }],
          ["search_commits", 230, { paths: ["services/payment-service"], since: "-24h" }],
          ["get_diff", 190, { sha: "09831d4393" }],
          ["get_diff", 170, { sha: "739d116b00" }],
          ["get_diff", 150, { sha: "4bccd960fb" }],
        ],
        status: "success",
        summary:
          "Commit 739d116 'tune db pool' lowered DB_POOL_SIZE from 20 to 2 and shipped in release payment-service/v1.8.2 at 09:55Z.",
        signals: ["risky_config_change", "recent_deployment_change"],
        evidence: evidence.code,
        findings: [
          {
            type: "risky_config_change",
            kind: "FACT",
            description:
              'DB_POOL_SIZE changed from "20" to "2" in services/payment-service/config/app.yaml.',
            evidence_ids: ["ev-s1-code-1"],
            confidence: 0.99,
          },
          {
            type: "recent_deployment_change",
            kind: "FACT",
            description: "The change shipped in payment-service/v1.8.2, released 09:55Z.",
            evidence_ids: ["ev-s1-code-2"],
            confidence: 0.97,
          },
        ],
      },
      {
        agent: "tickets",
        objective: "Find open incidents and known issues for payment-service",
        start: 1800,
        duration: 2600,
        tools: [
          [
            "jira_search",
            340,
            { jql: 'project = "OPS" AND labels in ("payment-service") ORDER BY updated DESC' },
          ],
          ["jira_search", 280, { jql: 'text ~ "connection pool" ORDER BY created DESC' }],
        ],
        status: "success",
        summary:
          "Known issue OPS-12 'payment-service DB connection timeouts' is open (High). OPS-2 is a resolved incident with the same cause.",
        signals: ["known_issue_open"],
        evidence: evidence.tickets,
        findings: [
          {
            type: "known_issue_open",
            kind: "FACT",
            description:
              "OPS-12 describes the same timeout error on /api/v1/pay and suspects pool sizing.",
            evidence_ids: ["ev-s1-tickets-1"],
            confidence: 0.9,
          },
          {
            type: "similar_past_incident",
            kind: "CORRELATION",
            description:
              "OPS-2 (resolved) was a pool exhaustion after a release, fixed by rollback.",
            evidence_ids: ["ev-s1-tickets-2"],
            confidence: 0.75,
          },
        ],
      },
      {
        agent: "knowledge",
        objective: "Search runbooks and known issues matching the symptoms",
        start: 1900,
        duration: 3900,
        tools: [
          ["list_docs", 120, {}],
          ["search", 260, { query: "database connection timeout pool" }],
          ["search", 240, { query: "HighErrorRate DatabaseConnectionPoolExhausted" }],
          ["get_doc", 150, { path: "knowledge-base/runbooks/database-connection-pool.md" }],
        ],
        status: "success",
        summary:
          "Runbook knowledge-base/runbooks/database-connection-pool.md ranks first and documents this exact failure (INC-2026-031) with a rollback mitigation.",
        signals: ["runbook_found", "known_issue_documented", "mitigation_available"],
        evidence: evidence.knowledge,
        findings: [
          {
            type: "runbook_found",
            kind: "FACT",
            description: "The DB pool runbook matches both log patterns and both firing alerts.",
            evidence_ids: ["ev-s1-knowledge-1", "ev-s1-knowledge-3"],
            confidence: 0.92,
          },
          {
            type: "known_issue_documented",
            kind: "CORRELATION",
            description: "INC-2026-031 in the runbook is the same v1.8.2 'tune db pool' change.",
            evidence_ids: ["ev-s1-knowledge-2"],
            confidence: 0.88,
          },
        ],
      },
      {
        agent: "logs",
        round: 2,
        objective:
          "Follow-up: confirm the timeouts started with the v1.8.2 pods and not before the rollout",
        start: 11600,
        duration: 2400,
        tools: [
          [
            "execute_esql",
            350,
            {
              index: "payment-prod-*",
              query: "WHERE @timestamp < 10:08Z AND message LIKE 'Database connection timeout*'",
            },
          ],
        ],
        status: "success",
        summary:
          "No pool timeouts before the 10:08Z rollout; the first one appears 2 minutes after the v1.8.2 pods started.",
        signals: ["db_timeout_errors_up"],
        evidence: [],
        findings: [
          {
            type: "onset_after_rollout",
            kind: "CORRELATION",
            description: "Zero timeouts between 10:00Z and 10:08Z; first at 10:10:07Z.",
            evidence_ids: ["ev-s1-logs-1", "ev-s1-logs-3", "ev-s1-k8s-1"],
            confidence: 0.93,
          },
        ],
      },
    ],
    hypotheses: [
      {
        id: "hy-s1-1",
        statement:
          "Database connection pool misconfiguration introduced in payment-service v1.8.2 (DB_POOL_SIZE 20 → 2) exhausts the pool; requests hit a connection timeout and return HTTP 500.",
        confidence: 0.91,
        supporting_evidence_ids: [
          "ev-s1-code-1",
          "ev-s1-k8s-1",
          "ev-s1-logs-1",
          "ev-s1-logs-3",
          "ev-s1-metrics-3",
          "ev-s1-metrics-1",
          "ev-s1-alerts-1",
          "ev-s1-knowledge-2",
          "ev-s1-tickets-1",
        ],
        contradicting_evidence_ids: [],
      },
      {
        id: "hy-s1-2",
        statement: "Postgres itself is overloaded or unreachable.",
        confidence: 0.12,
        supporting_evidence_ids: ["ev-s1-logs-1"],
        contradicting_evidence_ids: ["ev-s1-logs-3", "ev-s1-metrics-3"],
      },
    ],
    recommendations: [
      {
        id: "rec-s1-1",
        action:
          "Roll back payment-service to v1.8.1: kubectl rollout undo deployment/payment-service -n prod",
        rationale: "Restores DB_POOL_SIZE=20; the runbook's first mitigation step.",
        risk: "medium",
        requires_approval: true,
        evidence_ids: ["ev-s1-k8s-1", "ev-s1-knowledge-1"],
      },
      {
        id: "rec-s1-2",
        action: "Revert commit 739d116 'tune db pool' so the next deploy doesn't reintroduce it",
        rationale: "The config change is the root cause.",
        risk: "low",
        requires_approval: false,
        evidence_ids: ["ev-s1-code-1"],
      },
      {
        id: "rec-s1-3",
        action: "Watch the 5xx ratio and db_pool saturation return to baseline within 2–3 minutes",
        rationale: "Confirms the fix.",
        risk: "low",
        requires_approval: false,
        evidence_ids: ["ev-s1-metrics-1", "ev-s1-metrics-3"],
      },
      {
        id: "rec-s1-4",
        action: "Update OPS-12 with this RCA and require owner review for DB_POOL_* changes",
        rationale: "Prevents a repeat (runbook: Prevention).",
        risk: "low",
        requires_approval: true,
        evidence_ids: ["ev-s1-tickets-1"],
      },
    ],
    timeline: [
      {
        timestamp: "2026-09-28T08:20:00Z",
        description: "Commit 739d116 'tune db pool' sets DB_POOL_SIZE 20 → 2",
        source: "code",
        evidence_id: "ev-s1-code-1",
      },
      {
        timestamp: "2026-09-28T09:55:00Z",
        description: "Release payment-service/v1.8.2 tagged",
        source: "code",
        evidence_id: "ev-s1-code-2",
      },
      {
        timestamp: "2026-09-28T10:08:00Z",
        description: "Deployment rolls out revision 14 (v1.8.2)",
        source: "k8s",
        evidence_id: "ev-s1-k8s-1",
      },
      {
        timestamp: "2026-09-28T10:09:00Z",
        description: "DB pool saturates at 100% (2/2 active)",
        source: "metrics",
        evidence_id: "ev-s1-metrics-3",
      },
      {
        timestamp: "2026-09-28T10:10:07Z",
        description: "First 'Database connection timeout' error",
        source: "logs",
        evidence_id: "ev-s1-logs-1",
      },
      {
        timestamp: "2026-09-28T10:11:00Z",
        description: "Alert DatabaseConnectionPoolExhausted fires",
        source: "alerts",
        evidence_id: "ev-s1-alerts-1",
      },
      {
        timestamp: "2026-09-28T10:12:00Z",
        description: "Alert HighErrorRate fires (5xx 24.6%)",
        source: "alerts",
        evidence_id: "ev-s1-alerts-2",
      },
      {
        timestamp: "2026-09-28T10:30:00Z",
        description: "Investigation started",
        source: "aiops",
        evidence_id: null,
      },
    ],
    report: {
      summary:
        "payment-service has returned HTTP 500 on POST /api/v1/pay since 10:10Z. Release v1.8.2 lowered the database connection pool from 20 to 2 connections; the pool saturated one minute after the rollout and requests now time out waiting for a connection. Rolling back to v1.8.1 is the documented fix.",
      root_cause_hypothesis_id: "hy-s1-1",
      confidence: 0.91,
      impact:
        "HTTP 500 on POST /api/v1/pay for ~25% of requests since 10:10Z; p95 latency 9.5 s (45× baseline).",
      affected_services: ["payment-service", "order-service"],
      severity: "critical",
      next_steps: [
        "Roll back payment-service to v1.8.1 (kubectl rollout undo deployment/payment-service -n prod).",
        "Revert commit 739d116 'tune db pool' in git.",
        "Confirm the 5xx ratio and pool saturation return to baseline within 2–3 minutes.",
        "Update OPS-12 with this RCA; add owner review for DB_POOL_* changes.",
      ],
      open_questions: ["Why did the change pass review without a load test in staging?"],
    },
  };
}

// ---------------------------------------------------------------------------------------------
// S0: healthy baseline
// ---------------------------------------------------------------------------------------------
function s0(): ScenarioSpec {
  const created = at("2026-09-28T08:05:00Z");
  const ws = created - 30 * MIN;
  const we = created;
  return {
    id: "S0",
    title: "Healthy baseline (no incident)",
    description: "Normal traffic and background noise only. Agents must not hallucinate a problem.",
    question: "Is anything wrong with payment-service in production?",
    service: "payment-service",
    environment: "production",
    created,
    windowStart: ws,
    windowEnd: we,
    symptoms: [],
    rootCauseLabel: null,
    variants: [
      "Is anything wrong with payment-service in production?",
      "Health check: payments in prod",
      "Is user-service healthy right now?",
    ],
    agents: [
      {
        agent: "logs",
        objective: "Find new error patterns for payment-service in production",
        start: 1300,
        duration: 3900,
        tools: [
          ["execute_esql", 400],
          ["execute_esql", 520],
        ],
        status: "no_signal",
        summary:
          "Error volume matches the baseline (≈3 ERROR/h, all known patterns). No new patterns.",
        signals: [],
        evidence: [
          logPattern(
            "ev-s0-logs-1",
            "ERROR volume at baseline: 2 known 'card declined' warnings, no new pattern",
            {
              index: "payment-prod-*",
              pattern: "Card declined by issuer: <REASON>",
              level: "WARN",
              count: 2,
              baseline: 71,
              first: we - 22 * MIN,
              last: we - 4 * MIN,
              service: "payment-service",
              start: ws,
              end: we,
              query: "FROM payment-prod-* | STATS count BY pattern",
              samples: [
                "2026-09-28T07:43:10.201Z WARN [payment-service v1.8.1] Card declined by issuer: insufficient_funds",
              ],
            },
          ),
        ],
        findings: [
          {
            type: "no_new_patterns",
            kind: "OBSERVATION",
            description: "No new error pattern; volume within the 24 h baseline.",
            evidence_ids: ["ev-s0-logs-1"],
            confidence: 0.9,
          },
        ],
      },
      {
        agent: "metrics",
        objective: "Check error rate, latency and saturation against the baseline",
        start: 1400,
        duration: 6100,
        tools: [
          ["query_range", 300],
          ["query_range", 290],
          ["query_range", 310],
          ["query_range", 280],
        ],
        status: "no_signal",
        summary:
          "No metric anomaly for payment-service: error rate 0.4%, p95 0.2 s, pool 9%, Redis up.",
        signals: ["no_anomaly"],
        evidence: [
          metric("ev-s0-metrics-1", "5xx ratio", "5xx ratio steady at ≈0.4%", {
            metric: "http_5xx_ratio",
            unit: "percent",
            service: "payment-service",
            start: ws,
            end: we,
            anomalyStart: null,
            fn: () => 0.4,
            baseline: 0.4,
            peak: 0.6,
            panel: "Error rate",
            query: "5xx ratio",
            noise: 0.3,
          }),
          metric("ev-s0-metrics-2", "p95 latency", "p95 latency steady at ≈0.20 s", {
            metric: "http_request_duration_p95",
            unit: "seconds",
            service: "payment-service",
            start: ws,
            end: we,
            anomalyStart: null,
            fn: () => 0.2,
            baseline: 0.2,
            peak: 0.24,
            panel: "Latency p95",
            query: "histogram_quantile(0.95, …)",
          }),
        ],
        findings: [
          {
            type: "no_anomaly",
            kind: "OBSERVATION",
            description: "No metric anomaly: all series within the baseline band.",
            evidence_ids: ["ev-s0-metrics-1", "ev-s0-metrics-2"],
            confidence: 0.92,
          },
        ],
      },
      {
        agent: "alerts",
        objective: "List firing alerts for payment-service",
        start: 1350,
        duration: 1800,
        tools: [
          ["list_alerts", 170],
          ["list_silences", 80],
        ],
        status: "no_signal",
        summary: "No active alerts for payment-service (thresholds not breached).",
        signals: ["no_active_alerts"],
        evidence: [
          ev(
            "ev-s0-alerts-1",
            "alert",
            "alertmanager",
            "No active alerts for service=payment-service, namespace=prod",
            { ts: we, data: { alertname: null, state: "none", count: 0 } },
          ),
        ],
        findings: [
          {
            type: "no_active_alerts",
            kind: "FACT",
            description: "No active alerts; no thresholds breached.",
            evidence_ids: ["ev-s0-alerts-1"],
            confidence: 0.99,
          },
        ],
      },
      {
        agent: "k8s",
        objective: "Check the payment-service deployment and pods",
        start: 1500,
        duration: 3300,
        tools: [
          ["get_deployment", 230],
          ["list_pods", 200],
          ["list_events", 240],
        ],
        status: "no_signal",
        summary: "Nothing abnormal in Kubernetes: 1/1 ready, 0 restarts, last rollout 3 days ago.",
        signals: ["healthy"],
        evidence: [
          ev(
            "ev-s0-k8s-1",
            "k8s_event",
            "kubernetes",
            "payment-service 1/1 ready, 0 restarts, no warning events",
            { ts: we, data: { reason: "PodHealth", ready: "1/1", restarts: 0 } },
          ),
        ],
        findings: [
          {
            type: "healthy",
            kind: "FACT",
            description: "Nothing abnormal in Kubernetes.",
            evidence_ids: ["ev-s0-k8s-1"],
            confidence: 0.95,
          },
        ],
      },
      {
        agent: "code",
        objective: "Find recent changes to payment-service",
        start: 1550,
        duration: 2800,
        tools: [
          ["list_releases", 150],
          ["search_commits", 220],
        ],
        status: "no_signal",
        summary: "No changes to payment-service in the last 24 h.",
        signals: ["no_recent_changes"],
        evidence: [],
        findings: [],
      },
      {
        agent: "tickets",
        objective: "Find open incidents for payment-service",
        start: 1600,
        duration: 2100,
        tools: [["jira_search", 310]],
        status: "no_signal",
        summary: "No open incidents for payment-service.",
        signals: [],
        evidence: [],
        findings: [],
      },
      {
        agent: "knowledge",
        objective: "Search runbooks for the symptoms",
        start: 1650,
        duration: 1600,
        tools: [["list_docs", 110]],
        status: "no_signal",
        summary: "No symptoms to search for; no relevant runbook.",
        signals: ["no_relevant_docs"],
        evidence: [],
        findings: [],
      },
    ],
    hypotheses: [],
    recommendations: [
      {
        id: "rec-s0-1",
        action: "No action needed; keep monitoring",
        rationale: "All agents report a healthy baseline.",
        risk: "low",
        requires_approval: false,
        evidence_ids: ["ev-s0-metrics-1", "ev-s0-alerts-1"],
      },
    ],
    timeline: [
      {
        timestamp: iso(created),
        description: "Investigation started; no anomalies found",
        source: "aiops",
        evidence_id: null,
      },
    ],
    report: {
      summary:
        "No incident detected for payment-service in production. Error rate, latency, pods, alerts and recent changes are all at their baseline.",
      root_cause_hypothesis_id: null,
      confidence: 0,
      impact: "None observed.",
      affected_services: [],
      severity: "none",
      next_steps: ["No action needed."],
      open_questions: ["If users still report errors, which endpoint and since when?"],
    },
  };
}

// ---------------------------------------------------------------------------------------------
// S2: memory leak → OOMKilled (order-service)
// ---------------------------------------------------------------------------------------------
function s2(): ScenarioSpec {
  const created = at("2026-09-25T14:20:00Z");
  const ws = created - 30 * MIN;
  const we = created;
  const onset = at("2026-09-25T14:02:00Z");
  const saw = (t: number) => {
    if (t < onset) return 92;
    const period = 4 * MIN;
    const phase = ((t - onset) % period) / period;
    return 110 + 146 * phase;
  };
  return {
    id: "S2",
    title: "Memory leak -> OOM restarts (order-service)",
    description:
      "order-service leaks memory; the container is OOMKilled and restarts every ~4 minutes.",
    question: "Orders are failing intermittently in production",
    service: "order-service",
    environment: "production",
    created,
    windowStart: ws,
    windowEnd: we,
    symptoms: ["oom_errors", "pod_restarts"],
    rootCauseLabel: "Memory leak (OOMKilled)",
    variants: [
      "Orders are failing intermittently in production",
      "order-service keeps restarting",
      "503s on order creation",
    ],
    agents: [
      {
        agent: "logs",
        objective: "Find new error patterns for order-service",
        start: 1300,
        duration: 5600,
        tools: [
          ["execute_esql", 450],
          ["execute_esql", 580],
          ["execute_esql", 330],
        ],
        status: "success",
        summary:
          "java.lang.OutOfMemoryError (9) after 41 'GC overhead' warnings; the service restarted 5 times since 14:02Z.",
        signals: ["oom_errors"],
        evidence: [
          logPattern(
            "ev-s2-logs-1",
            "9 'java.lang.OutOfMemoryError: Java heap space' errors since 14:05Z",
            {
              index: "order-prod-*",
              pattern: "java.lang.OutOfMemoryError: Java heap space",
              level: "ERROR",
              count: 9,
              baseline: 0,
              first: at("2026-09-25T14:05:40Z"),
              last: at("2026-09-25T14:18:02Z"),
              service: "order-service",
              start: ws,
              end: we,
              query: "FROM order-prod-* | STATS count BY pattern",
              samples: [
                "2026-09-25T14:05:40.118Z ERROR [order-service v2.3.0] java.lang.OutOfMemoryError: Java heap space at OrderCache.put(OrderCache.java:42)",
              ],
            },
          ),
          logPattern(
            "ev-s2-logs-2",
            "41 'GC overhead limit exceeded' warnings, then 5 'Starting order-service' restarts",
            {
              index: "order-prod-*",
              pattern: "GC overhead limit exceeded (heap used <NUM>%)",
              level: "WARN",
              count: 41,
              baseline: 0,
              first: at("2026-09-25T14:03:10Z"),
              last: at("2026-09-25T14:19:30Z"),
              service: "order-service",
              start: ws,
              end: we,
              query: 'FROM order-prod-* | WHERE level == "WARN"',
              samples: [
                "2026-09-25T14:03:10.402Z WARN [order-service v2.3.0] GC overhead limit exceeded (heap used 97%)",
              ],
            },
          ),
        ],
        findings: [
          {
            type: "oom_errors",
            kind: "FACT",
            description: "9 OutOfMemoryError events, each followed by a restart.",
            evidence_ids: ["ev-s2-logs-1"],
            confidence: 0.97,
          },
          {
            type: "memory_pressure",
            kind: "OBSERVATION",
            description: "GC overhead warnings precede every OOM by 2–3 minutes.",
            evidence_ids: ["ev-s2-logs-2"],
            confidence: 0.9,
          },
        ],
      },
      {
        agent: "metrics",
        objective: "Check memory, error rate and throughput for order-service",
        start: 1400,
        duration: 7200,
        tools: [
          ["query_range", 320],
          ["query_range", 300],
          ["query_range", 340],
          ["query_range", 310],
        ],
        status: "success",
        summary:
          "order-service memory rss up in a sawtooth to the 256 Mi limit every ~4 min (OOMKilled), traffic drops during restarts; 5xx 0.3% → 8%.",
        signals: ["memory_pressure", "traffic_drop"],
        evidence: [
          metric(
            "ev-s2-metrics-1",
            "Memory RSS",
            "Memory climbs 110 → 256 Mi and resets every ~4 minutes (sawtooth = leak + OOMKilled)",
            {
              metric: "process_resident_memory_mb",
              unit: "bytes",
              service: "order-service",
              start: ws,
              end: we,
              anomalyStart: onset,
              fn: saw,
              baseline: 92,
              peak: 256,
              panel: "Memory",
              query: 'max(process_resident_memory_bytes{service="order-service"})',
              noise: 0.02,
            },
          ),
          metric("ev-s2-metrics-2", "5xx ratio", "5xx ratio 0.3% → 8% in bursts during restarts", {
            metric: "http_5xx_ratio",
            unit: "percent",
            service: "order-service",
            start: ws,
            end: we,
            anomalyStart: onset,
            fn: (t) => (t < onset ? 0.3 : 0.3 + 7.7 * (saw(t) > 235 ? 1 : 0.15)),
            baseline: 0.3,
            peak: 8,
            panel: "Error rate",
            query: "5xx ratio",
            noise: 0.15,
          }),
          metric("ev-s2-metrics-3", "Throughput", "Throughput drops to ≈0 during each restart", {
            metric: "http_requests_rate",
            unit: "rps",
            service: "order-service",
            start: ws,
            end: we,
            anomalyStart: onset,
            fn: (t) => (t >= onset && saw(t) > 245 ? 0.3 : 4.2),
            baseline: 4.2,
            peak: 4.6,
            panel: "Throughput",
            query: 'sum(rate(http_requests_total{service="order-service"}[2m]))',
            noise: 0.08,
          }),
        ],
        findings: [
          {
            type: "memory_pressure",
            kind: "FACT",
            description: "Memory rss up to the 256 Mi limit, resetting every ~4 minutes.",
            evidence_ids: ["ev-s2-metrics-1"],
            confidence: 0.95,
          },
          {
            type: "traffic_drop",
            kind: "OBSERVATION",
            description: "Traffic drops to ≈0 during restarts; 5xx bursts at the same times.",
            evidence_ids: ["ev-s2-metrics-2", "ev-s2-metrics-3"],
            confidence: 0.88,
          },
        ],
      },
      {
        agent: "alerts",
        objective: "List firing alerts for order-service",
        start: 1350,
        duration: 2000,
        tools: [
          ["list_alerts", 190],
          ["list_silences", 90],
        ],
        status: "success",
        summary: "PodOOMKilled (critical) and PodCrashLooping (warning) firing for order-service.",
        signals: ["alerts_firing", "critical_alert_firing"],
        evidence: [
          ev(
            "ev-s2-alerts-1",
            "alert",
            "alertmanager",
            "PodOOMKilled (critical) firing since 14:06Z",
            {
              ts: at("2026-09-25T14:06:00Z"),
              link: L.alertmanager("PodOOMKilled"),
              data: {
                alertname: "PodOOMKilled",
                severity: "critical",
                state: "active",
                starts_at: "2026-09-25T14:06:00Z",
                runbook_url: "knowledge-base/runbooks/memory-leak-oom.md",
              },
            },
          ),
          ev(
            "ev-s2-alerts-2",
            "alert",
            "alertmanager",
            "PodCrashLooping (warning) firing since 14:10Z",
            {
              ts: at("2026-09-25T14:10:00Z"),
              link: L.alertmanager("PodCrashLooping"),
              data: {
                alertname: "PodCrashLooping",
                severity: "warning",
                state: "active",
                starts_at: "2026-09-25T14:10:00Z",
                runbook_url: "knowledge-base/runbooks/pod-crashloop.md",
              },
            },
          ),
        ],
        findings: [
          {
            type: "alerts_firing",
            kind: "FACT",
            description: "PodOOMKilled and PodCrashLooping are firing.",
            evidence_ids: ["ev-s2-alerts-1", "ev-s2-alerts-2"],
            confidence: 0.99,
          },
        ],
      },
      {
        agent: "k8s",
        objective: "Check order-service pods and events",
        start: 1500,
        duration: 4400,
        tools: [
          ["get_deployment", 250],
          ["list_pods", 230],
          ["list_events", 270],
        ],
        status: "success",
        summary:
          "Container app OOMKilled (exit 137) with 5 restarts; back-off restarting failed container. Memory limit 256 Mi.",
        signals: ["oom_killed", "pod_restarts"],
        evidence: [
          ev(
            "ev-s2-k8s-1",
            "k8s_event",
            "kubernetes",
            "Pod order-service-65c484cb48-j62fc: last state OOMKilled (exit 137), restart_count 5",
            {
              ts: at("2026-09-25T14:06:09Z"),
              link: L.grafanaK8s("order-service", ws, we),
              data: {
                reason: "OOMKilled",
                object: "pod/order-service-65c484cb48-j62fc",
                exit_code: 137,
                restarts: 5,
                limit: "256Mi",
              },
            },
          ),
          ev(
            "ev-s2-k8s-2",
            "k8s_event",
            "kubernetes",
            "BackOff: back-off 20s restarting failed container app",
            {
              ts: at("2026-09-25T14:10:12Z"),
              data: {
                reason: "BackOff",
                object: "pod/order-service-65c484cb48-j62fc",
                type: "Warning",
                count: 4,
              },
            },
          ),
        ],
        findings: [
          {
            type: "oom_killed",
            kind: "FACT",
            description: "OOMKilled (exit 137), 5 restarts in 18 minutes.",
            evidence_ids: ["ev-s2-k8s-1", "ev-s2-k8s-2"],
            confidence: 0.98,
          },
        ],
      },
      {
        agent: "code",
        objective: "Find recent changes to order-service",
        start: 1600,
        duration: 4700,
        tools: [
          ["list_releases", 170],
          ["search_commits", 240],
          ["get_diff", 200],
        ],
        status: "success",
        summary:
          "order-service v2.3.0 added an in-process order cache without a size bound (ORDER_CACHE_MAX_ENTRIES 10000 → 0 = unbounded).",
        signals: ["risky_config_change", "recent_deployment_change"],
        evidence: [
          ev(
            "ev-s2-code-1",
            "commit",
            "git",
            "Commit 5e1c0a2 'cache order lookups in memory' makes the cache unbounded",
            {
              ts: at("2026-09-25T12:40:00Z"),
              data: {
                sha: "5e1c0a2f7b9d4e3a1c6b8d0e2f4a6c8e0b2d4f6a",
                short_sha: "5e1c0a2f7b",
                author: "Aiko Tanaka",
                subject: "cache order lookups in memory",
                risky: true,
                files: [
                  {
                    path: "services/order-service/config/app.yaml",
                    status: "modified",
                    hunk: '@@ -5,6 +5,7 @@\n   DB_POOL_SIZE: "20"\n-  ORDER_CACHE_MAX_ENTRIES: "10000"\n+  ORDER_CACHE_MAX_ENTRIES: "0"  # 0 = unbounded\n+  ORDER_CACHE_TTL_S: "86400"\n   MEMORY_LIMIT_MB: "256"',
                  },
                ],
              },
            },
          ),
        ],
        findings: [
          {
            type: "risky_config_change",
            kind: "FACT",
            description: "The order cache became unbounded in v2.3.0 (released 13:50Z).",
            evidence_ids: ["ev-s2-code-1"],
            confidence: 0.9,
          },
        ],
      },
      {
        agent: "tickets",
        objective: "Find known issues for order-service",
        start: 1650,
        duration: 2200,
        tools: [["jira_search", 320]],
        status: "success",
        summary: "OPS-21 'order-service restarts under load' is open.",
        signals: ["known_issue_open"],
        evidence: [
          ev(
            "ev-s2-tickets-1",
            "ticket",
            "jira",
            "Open OPS-21 'order-service restarts under load' (Medium)",
            {
              ts: at("2026-09-24T09:00:00Z"),
              link: L.ticket("OPS-21"),
              data: {
                key: "OPS-21",
                status: "Open",
                priority: "Medium",
                summary: "order-service restarts under load",
              },
            },
          ),
        ],
        findings: [
          {
            type: "known_issue_open",
            kind: "CORRELATION",
            description: "OPS-21 reports the same restarts.",
            evidence_ids: ["ev-s2-tickets-1"],
            confidence: 0.7,
          },
        ],
      },
      {
        agent: "knowledge",
        objective: "Search runbooks for OOM symptoms",
        start: 1700,
        duration: 3500,
        tools: [
          ["search", 250],
          ["get_doc", 160],
        ],
        status: "success",
        summary:
          "Runbook knowledge-base/runbooks/memory-leak-oom.md matches (OOMKilled, GC overhead).",
        signals: ["runbook_found", "known_issue_documented", "mitigation_available"],
        evidence: [
          ev(
            "ev-s2-knowledge-1",
            "doc",
            "knowledge-base",
            "Runbook 'Memory leak and OOMKilled': roll back, then bound caches",
            {
              link: L.runbook("memory-leak-oom.md", "mitigation"),
              data: {
                path: "knowledge-base/runbooks/memory-leak-oom.md",
                title: "Memory leak and OOMKilled",
                section: "Mitigation",
                rank: 1,
                score: 0.9,
                excerpt:
                  "Roll back the release that introduced the growth. Temporarily raise the memory limit only to buy time; unbounded caches must get a max size.",
              },
            },
          ),
        ],
        findings: [
          {
            type: "runbook_found",
            kind: "FACT",
            description: "memory-leak-oom.md is the top match.",
            evidence_ids: ["ev-s2-knowledge-1"],
            confidence: 0.9,
          },
        ],
      },
    ],
    hypotheses: [
      {
        id: "hy-s2-1",
        statement:
          "Memory leak in order-service v2.3.0 (unbounded in-process order cache) causes OutOfMemoryError and repeated OOMKilled restarts; requests fail with 503 during restarts.",
        confidence: 0.88,
        supporting_evidence_ids: [
          "ev-s2-metrics-1",
          "ev-s2-k8s-1",
          "ev-s2-logs-1",
          "ev-s2-code-1",
          "ev-s2-alerts-1",
          "ev-s2-knowledge-1",
        ],
        contradicting_evidence_ids: [],
      },
      {
        id: "hy-s2-2",
        statement: "The memory limit (256 Mi) is simply too low for normal load.",
        confidence: 0.14,
        supporting_evidence_ids: ["ev-s2-k8s-1"],
        contradicting_evidence_ids: ["ev-s2-metrics-3"],
      },
    ],
    recommendations: [
      {
        id: "rec-s2-1",
        action: "Roll back order-service to v2.2.1",
        rationale: "Removes the unbounded cache.",
        risk: "medium",
        requires_approval: true,
        evidence_ids: ["ev-s2-code-1"],
      },
      {
        id: "rec-s2-2",
        action: "Set ORDER_CACHE_MAX_ENTRIES back to 10000 before the next release",
        rationale: "Bounds the cache.",
        risk: "low",
        requires_approval: false,
        evidence_ids: ["ev-s2-code-1", "ev-s2-knowledge-1"],
      },
    ],
    timeline: [
      {
        timestamp: "2026-09-25T13:50:00Z",
        description: "order-service v2.3.0 rolled out (unbounded order cache)",
        source: "code",
        evidence_id: "ev-s2-code-1",
      },
      {
        timestamp: "2026-09-25T14:02:00Z",
        description: "Memory starts climbing in a sawtooth",
        source: "metrics",
        evidence_id: "ev-s2-metrics-1",
      },
      {
        timestamp: "2026-09-25T14:03:10Z",
        description: "First 'GC overhead limit exceeded' warning",
        source: "logs",
        evidence_id: "ev-s2-logs-2",
      },
      {
        timestamp: "2026-09-25T14:05:40Z",
        description: "First OutOfMemoryError",
        source: "logs",
        evidence_id: "ev-s2-logs-1",
      },
      {
        timestamp: "2026-09-25T14:06:09Z",
        description: "Container OOMKilled (exit 137)",
        source: "k8s",
        evidence_id: "ev-s2-k8s-1",
      },
      {
        timestamp: "2026-09-25T14:10:00Z",
        description: "PodCrashLooping fires",
        source: "alerts",
        evidence_id: "ev-s2-alerts-2",
      },
    ],
    report: {
      summary:
        "order-service is restarting every ~4 minutes because it runs out of memory. Release v2.3.0 made the in-process order cache unbounded, so memory grows until the container is OOMKilled; requests fail with 503 during each restart.",
      root_cause_hypothesis_id: "hy-s2-1",
      confidence: 0.88,
      impact: "Intermittent 503 on order creation (≈8% of requests during restarts) since 14:02Z.",
      affected_services: ["order-service"],
      severity: "high",
      next_steps: [
        "Roll back order-service to v2.2.1.",
        "Bound the order cache (ORDER_CACHE_MAX_ENTRIES=10000).",
        "Close OPS-21 with this RCA.",
      ],
      open_questions: [],
    },
  };
}

// ---------------------------------------------------------------------------------------------
// S3: slow downstream dependency (inventory-service → order-service)
// ---------------------------------------------------------------------------------------------
function s3(): ScenarioSpec {
  const created = at("2026-09-26T11:15:00Z");
  const ws = created - 30 * MIN;
  const we = created;
  const onset = at("2026-09-26T10:58:00Z");
  return {
    id: "S3",
    title: "Slow downstream dependency (inventory-service -> order-service)",
    description: "Slow queries in inventory-service make order-service calls time out (504).",
    question: "Why are orders timing out in production?",
    service: "order-service",
    environment: "production",
    created,
    windowStart: ws,
    windowEnd: we,
    symptoms: ["dependency_timeouts", "latency_up"],
    rootCauseLabel: "Slow dependency (inventory)",
    variants: [
      "Why are orders timing out in production?",
      "Order checkout returns 504",
      "order-service latency spike",
    ],
    agents: [
      {
        agent: "logs",
        objective: "Find new error patterns for order-service",
        start: 1300,
        duration: 5100,
        tools: [
          ["execute_esql", 430],
          ["execute_esql", 560],
        ],
        status: "success",
        summary:
          "57 'Upstream timeout calling inventory-service after 3000ms' errors (HTTP 504) since 10:58Z.",
        signals: ["dependency_timeouts"],
        evidence: [
          logPattern(
            "ev-s3-logs-1",
            "57 upstream timeouts calling inventory-service GET /api/v1/stock (HTTP 504)",
            {
              index: "order-prod-*",
              pattern:
                "Upstream timeout calling inventory-service GET /api/v1/stock/<ID> after <NUM>ms",
              level: "ERROR",
              count: 57,
              baseline: 1,
              first: at("2026-09-26T10:58:21Z"),
              last: at("2026-09-26T11:14:50Z"),
              service: "order-service",
              start: ws,
              end: we,
              query: "FROM order-prod-* | STATS count BY pattern",
              samples: [
                "2026-09-26T10:58:21.004Z ERROR [order-service v2.3.0] Upstream timeout calling inventory-service GET /api/v1/stock/SKU-1042 after 3000ms",
              ],
            },
          ),
        ],
        findings: [
          {
            type: "dependency_timeouts",
            kind: "FACT",
            description: "57 upstream timeouts to inventory-service since 10:58Z.",
            evidence_ids: ["ev-s3-logs-1"],
            confidence: 0.96,
          },
        ],
      },
      {
        agent: "metrics",
        objective: "Check latency and errors for order-service and its dependencies",
        start: 1400,
        duration: 7000,
        tools: [
          ["query_range", 300],
          ["query_range", 320],
          ["query_range", 310],
        ],
        status: "success",
        summary:
          "order-service latency p95 up 0.12 s → 3.1 s and 5xx 0.2% → 11%; inventory-service latency p95 up 0.04 s → 4.2 s.",
        signals: ["latency_up", "dependency_latency_up", "error_rate_up"],
        evidence: [
          metric(
            "ev-s3-metrics-1",
            "p95 latency",
            "order-service p95 0.12 s → 3.1 s (capped by the 3 s client timeout)",
            {
              metric: "http_request_duration_p95",
              unit: "seconds",
              service: "order-service",
              start: ws,
              end: we,
              anomalyStart: onset,
              fn: (t) => 0.12 + 2.98 * ramp(t, onset - MIN, onset + 2 * MIN),
              baseline: 0.12,
              peak: 3.1,
              panel: "Latency p95",
              query: "histogram_quantile(0.95, …order-service…)",
              extra: [
                {
                  label: "inventory-service",
                  fn: (t) => 0.04 + 4.16 * ramp(t, onset - 2 * MIN, onset + MIN),
                },
              ],
            },
          ),
          metric("ev-s3-metrics-2", "5xx ratio", "order-service 5xx ratio 0.2% → 11% (504s)", {
            metric: "http_5xx_ratio",
            unit: "percent",
            service: "order-service",
            start: ws,
            end: we,
            anomalyStart: onset,
            fn: (t) => 0.2 + 10.8 * ramp(t, onset, onset + 3 * MIN),
            baseline: 0.2,
            peak: 11,
            panel: "Error rate",
            query: "5xx ratio",
            noise: 0.12,
          }),
        ],
        findings: [
          {
            type: "dependency_latency_up",
            kind: "FACT",
            description:
              "inventory-service latency p95 up 0.04 s → 4.2 s, starting a minute before order-service.",
            evidence_ids: ["ev-s3-metrics-1"],
            confidence: 0.94,
          },
          {
            type: "error_rate_up",
            kind: "FACT",
            description: "order-service 5xx 0.2% → 11%.",
            evidence_ids: ["ev-s3-metrics-2"],
            confidence: 0.93,
          },
        ],
      },
      {
        agent: "alerts",
        objective: "List firing alerts for order-service and dependencies",
        start: 1350,
        duration: 2100,
        tools: [
          ["list_alerts", 200],
          ["list_silences", 80],
        ],
        status: "success",
        summary:
          "HighLatencyP95 on inventory-service (dependency) and HighErrorRate on order-service are firing.",
        signals: ["alerts_firing", "critical_alert_firing", "dependency_alert_firing"],
        evidence: [
          ev(
            "ev-s3-alerts-1",
            "alert",
            "alertmanager",
            "HighLatencyP95 (warning) on inventory-service since 10:57Z",
            {
              ts: at("2026-09-26T10:57:00Z"),
              link: L.alertmanager("HighLatencyP95"),
              data: {
                alertname: "HighLatencyP95",
                severity: "warning",
                state: "active",
                starts_at: "2026-09-26T10:57:00Z",
                labels: { service: "inventory-service" },
              },
            },
          ),
          ev(
            "ev-s3-alerts-2",
            "alert",
            "alertmanager",
            "HighErrorRate (critical) on order-service since 11:00Z",
            {
              ts: at("2026-09-26T11:00:00Z"),
              link: L.alertmanager("HighErrorRate"),
              data: {
                alertname: "HighErrorRate",
                severity: "critical",
                state: "active",
                starts_at: "2026-09-26T11:00:00Z",
                labels: { service: "order-service" },
              },
            },
          ),
        ],
        findings: [
          {
            type: "dependency_alert_firing",
            kind: "CORRELATION",
            description:
              "The dependency's latency alert fired 3 minutes before order-service's error alert.",
            evidence_ids: ["ev-s3-alerts-1", "ev-s3-alerts-2"],
            confidence: 0.9,
          },
        ],
      },
      {
        agent: "k8s",
        objective: "Check order-service and dependency workloads",
        start: 1500,
        duration: 3600,
        tools: [
          ["get_deployment", 240],
          ["list_deployments", 190],
          ["list_events", 250],
        ],
        status: "success",
        summary: "order-service pods healthy; inventory-service rolled out v1.4.2 at 10:55Z.",
        signals: ["healthy", "dependency_rollout"],
        evidence: [
          ev(
            "ev-s3-k8s-1",
            "k8s_event",
            "kubernetes",
            "inventory-service rolled out revision 9 (v1.4.2) at 10:55Z",
            {
              ts: at("2026-09-26T10:55:00Z"),
              link: L.grafanaK8s("inventory-service", ws, we),
              data: {
                reason: "RolloutRevision",
                object: "deployment/inventory-service",
                revision: "9",
                change_cause: "v1.4.2",
              },
            },
          ),
        ],
        findings: [
          {
            type: "dependency_rollout",
            kind: "FACT",
            description: "inventory-service v1.4.2 rolled out 3 minutes before the timeouts.",
            evidence_ids: ["ev-s3-k8s-1"],
            confidence: 0.9,
          },
        ],
      },
      {
        agent: "code",
        objective: "Find recent changes to order-service and its dependencies",
        start: 1600,
        duration: 4600,
        tools: [
          ["list_releases", 160],
          ["search_commits", 250],
          ["get_diff", 210],
        ],
        status: "success",
        summary: "inventory-service v1.4.2 dropped index idx_stock_levels_sku in a migration.",
        signals: ["dependency_service_change"],
        evidence: [
          ev(
            "ev-s3-code-1",
            "commit",
            "git",
            "Commit 3c6b3f4 'migrate stock_levels indexes' drops idx_stock_levels_sku",
            {
              ts: at("2026-09-26T10:30:00Z"),
              data: {
                sha: "3c6b3f4c6ec3e7480408b3aacf2544dd9ba5f4ce",
                short_sha: "3c6b3f4c6e",
                author: "Jordan Lee",
                subject: "migrate stock_levels indexes",
                risky: true,
                files: [
                  {
                    path: "services/inventory-service/migrations/0007_stock_indexes.sql",
                    status: "added",
                    hunk: "@@ -0,0 +1,4 @@\n+-- replace the single-column index with a composite one\n+DROP INDEX IF EXISTS idx_stock_levels_sku;\n+-- TODO: CREATE INDEX idx_stock_levels_sku_wh ON stock_levels (sku, warehouse_id);\n+",
                  },
                ],
              },
            },
          ),
        ],
        findings: [
          {
            type: "dependency_service_change",
            kind: "FACT",
            description: "idx_stock_levels_sku was dropped and its replacement was never created.",
            evidence_ids: ["ev-s3-code-1"],
            confidence: 0.86,
          },
        ],
      },
      {
        agent: "tickets",
        objective: "Find known issues for order-service and dependencies",
        start: 1650,
        duration: 2300,
        tools: [["jira_search", 330]],
        status: "success",
        summary: "OPS-31 'inventory stock lookups slow' is open.",
        signals: ["dependency_known_issue_open"],
        evidence: [
          ev(
            "ev-s3-tickets-1",
            "ticket",
            "jira",
            "Open OPS-31 'inventory stock lookups slow' (High)",
            {
              ts: at("2026-09-26T11:05:00Z"),
              link: L.ticket("OPS-31"),
              data: {
                key: "OPS-31",
                status: "Open",
                priority: "High",
                summary: "inventory stock lookups slow",
              },
            },
          ),
        ],
        findings: [
          {
            type: "dependency_known_issue_open",
            kind: "CORRELATION",
            description: "OPS-31 reports slow inventory stock lookups.",
            evidence_ids: ["ev-s3-tickets-1"],
            confidence: 0.75,
          },
        ],
      },
      {
        agent: "knowledge",
        objective: "Search runbooks for dependency timeouts",
        start: 1700,
        duration: 3300,
        tools: [
          ["search", 260],
          ["get_doc", 150],
        ],
        status: "success",
        summary: "Runbook knowledge-base/runbooks/dependency-timeouts.md matches.",
        signals: ["runbook_found", "known_issue_documented", "mitigation_available"],
        evidence: [
          ev(
            "ev-s3-knowledge-1",
            "doc",
            "knowledge-base",
            "Runbook 'Downstream dependency timeouts': find the slow dependency, fix it there",
            {
              link: L.runbook("dependency-timeouts.md", "diagnosis"),
              data: {
                path: "knowledge-base/runbooks/dependency-timeouts.md",
                title: "Downstream dependency timeouts",
                section: "Diagnosis",
                rank: 1,
                score: 0.87,
                excerpt:
                  "The symptom is in the caller, the cause is in the dependency. Compare the dependency's p95 with the caller's timeout.",
              },
            },
          ),
        ],
        findings: [
          {
            type: "runbook_found",
            kind: "FACT",
            description: "dependency-timeouts.md is the top match.",
            evidence_ids: ["ev-s3-knowledge-1"],
            confidence: 0.87,
          },
        ],
      },
      {
        agent: "logs",
        round: 2,
        objective: "Follow-up: find slow queries in inventory-service",
        start: 10400,
        duration: 3800,
        tools: [["execute_esql", 480, { index: "inventory-prod-*" }]],
        status: "success",
        summary:
          "inventory-service logs 132 'slow query stock_levels took 3–5 s' warnings since 10:56Z.",
        signals: ["slow_queries"],
        evidence: [
          logPattern(
            "ev-s3-logs-2",
            "132 'slow query on stock_levels' warnings in inventory-service (3–5 s each)",
            {
              index: "inventory-prod-*",
              pattern: "slow query stock_levels took <NUM>ms (seq scan)",
              level: "WARN",
              count: 132,
              baseline: 0,
              first: at("2026-09-26T10:56:02Z"),
              last: at("2026-09-26T11:14:58Z"),
              service: "inventory-service",
              start: ws,
              end: we,
              query: 'FROM inventory-prod-* | WHERE message LIKE "slow query*"',
              samples: [
                "2026-09-26T10:56:02.771Z WARN [inventory-service v1.4.2] slow query stock_levels took 4210ms (seq scan)",
              ],
            },
          ),
        ],
        findings: [
          {
            type: "slow_queries",
            kind: "FACT",
            description: "stock_levels queries take 3–5 s (sequential scans) since v1.4.2.",
            evidence_ids: ["ev-s3-logs-2"],
            confidence: 0.93,
          },
        ],
      },
      {
        agent: "metrics",
        round: 2,
        objective: "Follow-up: confirm inventory-service latency leads order-service",
        start: 10500,
        duration: 3000,
        tools: [["query_range", 310, { service: "inventory-service" }]],
        status: "success",
        summary: "inventory-service latency rose ~60 s before order-service's.",
        signals: ["dependency_latency_up"],
        evidence: [],
        findings: [
          {
            type: "lead_lag",
            kind: "CORRELATION",
            description: "inventory-service latency leads order-service by ~60 s.",
            evidence_ids: ["ev-s3-metrics-1"],
            confidence: 0.85,
          },
        ],
      },
    ],
    hypotheses: [
      {
        id: "hy-s3-1",
        statement:
          "Slow stock_levels queries in inventory-service v1.4.2 (idx_stock_levels_sku dropped) make order-service calls exceed their 3 s timeout, returning HTTP 504.",
        confidence: 0.84,
        supporting_evidence_ids: [
          "ev-s3-logs-2",
          "ev-s3-metrics-1",
          "ev-s3-code-1",
          "ev-s3-k8s-1",
          "ev-s3-logs-1",
          "ev-s3-alerts-1",
        ],
        contradicting_evidence_ids: [],
      },
      {
        id: "hy-s3-2",
        statement: "order-service itself regressed.",
        confidence: 0.08,
        supporting_evidence_ids: ["ev-s3-metrics-2"],
        contradicting_evidence_ids: ["ev-s3-k8s-1"],
      },
    ],
    recommendations: [
      {
        id: "rec-s3-1",
        action: "Recreate the index on stock_levels (sku, warehouse_id) in inventory-service",
        rationale: "Removes the sequential scans.",
        risk: "medium",
        requires_approval: true,
        evidence_ids: ["ev-s3-code-1"],
      },
      {
        id: "rec-s3-2",
        action: "Or roll back inventory-service to v1.4.1",
        rationale: "Restores the old index.",
        risk: "medium",
        requires_approval: true,
        evidence_ids: ["ev-s3-k8s-1"],
      },
    ],
    timeline: [
      {
        timestamp: "2026-09-26T10:55:00Z",
        description: "inventory-service v1.4.2 rolled out",
        source: "k8s",
        evidence_id: "ev-s3-k8s-1",
      },
      {
        timestamp: "2026-09-26T10:56:02Z",
        description: "First slow stock_levels query",
        source: "logs",
        evidence_id: "ev-s3-logs-2",
      },
      {
        timestamp: "2026-09-26T10:57:00Z",
        description: "HighLatencyP95 fires on inventory-service",
        source: "alerts",
        evidence_id: "ev-s3-alerts-1",
      },
      {
        timestamp: "2026-09-26T10:58:21Z",
        description: "First order-service upstream timeout (504)",
        source: "logs",
        evidence_id: "ev-s3-logs-1",
      },
      {
        timestamp: "2026-09-26T11:00:00Z",
        description: "HighErrorRate fires on order-service",
        source: "alerts",
        evidence_id: "ev-s3-alerts-2",
      },
    ],
    report: {
      summary:
        "Orders time out because inventory-service became slow. Its v1.4.2 migration dropped the stock_levels index, so stock lookups take 3–5 s and order-service's 3 s client timeout returns HTTP 504.",
      root_cause_hypothesis_id: "hy-s3-1",
      confidence: 0.84,
      impact: "HTTP 504 on order creation for ~11% of requests since 10:58Z.",
      affected_services: ["order-service", "inventory-service"],
      severity: "high",
      next_steps: [
        "Recreate the stock_levels index in inventory-service (or roll back to v1.4.1).",
        "Update OPS-31 with this RCA.",
      ],
      open_questions: ["Should order-service add a circuit breaker for inventory calls?"],
    },
  };
}

// ---------------------------------------------------------------------------------------------
// S4: bad deployment (user-service image cannot be pulled)
// ---------------------------------------------------------------------------------------------
function s4(): ScenarioSpec {
  const created = at("2026-09-24T16:40:00Z");
  const ws = created - 30 * MIN;
  const we = created;
  const rollout = at("2026-09-24T16:21:00Z");
  return {
    id: "S4",
    title: "Bad deployment (user-service image cannot be pulled)",
    description:
      "user-service rollout references a nonexistent image tag; only 1 of 3 replicas is ready.",
    question: "Login and checkout requests are failing in production",
    service: "user-service",
    environment: "production",
    created,
    windowStart: ws,
    windowEnd: we,
    symptoms: ["capacity_degraded"],
    rootCauseLabel: "Bad image tag (ImagePullBackOff)",
    variants: [
      "Login and checkout requests are failing in production",
      "Users can't log in",
      "user-service 503 no healthy upstream",
    ],
    agents: [
      {
        agent: "logs",
        objective: "Find new error patterns for user-service",
        start: 1300,
        duration: 4800,
        tools: [
          ["execute_esql", 420],
          ["execute_esql", 510],
        ],
        status: "success",
        summary:
          "Request queue pressure on the single ready replica; callers log 38 '503 no healthy upstream'.",
        signals: ["capacity_degraded"],
        evidence: [
          logPattern(
            "ev-s4-logs-1",
            "38 '503 no healthy upstream' from callers; queue depth warnings on the one ready replica",
            {
              index: "user-prod-*",
              pattern: "Request queue depth <NUM> exceeds threshold (replicas ready=<NUM>)",
              level: "WARN",
              count: 38,
              baseline: 0,
              first: at("2026-09-24T16:23:40Z"),
              last: at("2026-09-24T16:39:12Z"),
              service: "user-service",
              start: ws,
              end: we,
              query: "FROM user-prod-* | STATS count BY pattern",
              samples: [
                "2026-09-24T16:23:40.210Z WARN [user-service v3.1.4] Request queue depth 64 exceeds threshold (replicas ready=1)",
              ],
            },
          ),
        ],
        findings: [
          {
            type: "capacity_degraded",
            kind: "OBSERVATION",
            description:
              "Only one replica serves traffic; queue depth warnings and 503s from callers.",
            evidence_ids: ["ev-s4-logs-1"],
            confidence: 0.85,
          },
        ],
      },
      {
        agent: "metrics",
        objective: "Check latency and errors for user-service",
        start: 1400,
        duration: 5600,
        tools: [
          ["query_range", 300],
          ["query_range", 290],
        ],
        status: "partial",
        summary:
          "user-service latency slightly elevated (p95 0.03 s → 0.4 s); no strong anomaly on its own.",
        signals: [],
        evidence: [
          metric(
            "ev-s4-metrics-1",
            "p95 latency",
            "user-service p95 0.03 s → 0.4 s on the remaining replica",
            {
              metric: "http_request_duration_p95",
              unit: "seconds",
              service: "user-service",
              start: ws,
              end: we,
              anomalyStart: rollout + 2 * MIN,
              fn: (t) => 0.03 + 0.37 * ramp(t, rollout, rollout + 4 * MIN),
              baseline: 0.03,
              peak: 0.4,
              panel: "Latency p95",
              query: "histogram_quantile(0.95, …user-service…)",
            },
          ),
        ],
        findings: [
          {
            type: "latency_up",
            kind: "OBSERVATION",
            description: "Moderate latency increase on user-service.",
            evidence_ids: ["ev-s4-metrics-1"],
            confidence: 0.6,
          },
        ],
      },
      {
        agent: "alerts",
        objective: "List firing alerts for user-service",
        start: 1350,
        duration: 1900,
        tools: [
          ["list_alerts", 180],
          ["list_silences", 80],
        ],
        status: "success",
        summary: "DeploymentReplicasMismatch (warning) firing for user-service: 1/3 available.",
        signals: ["alerts_firing"],
        evidence: [
          ev(
            "ev-s4-alerts-1",
            "alert",
            "alertmanager",
            "DeploymentReplicasMismatch (warning) since 16:31Z: 1/3 replicas available",
            {
              ts: at("2026-09-24T16:31:00Z"),
              link: L.alertmanager("DeploymentReplicasMismatch"),
              data: {
                alertname: "DeploymentReplicasMismatch",
                severity: "warning",
                state: "active",
                starts_at: "2026-09-24T16:31:00Z",
              },
            },
          ),
        ],
        findings: [
          {
            type: "alerts_firing",
            kind: "FACT",
            description: "DeploymentReplicasMismatch is firing.",
            evidence_ids: ["ev-s4-alerts-1"],
            confidence: 0.97,
          },
        ],
      },
      {
        agent: "k8s",
        objective: "Check user-service rollout, pods and events",
        start: 1500,
        duration: 4200,
        tools: [
          ["get_deployment", 250],
          ["list_pods", 240],
          ["list_events", 260],
        ],
        status: "success",
        summary:
          "Rollout to aiops/user-service:v3.2.0 is stuck: 2 pods in ImagePullBackOff (manifest unknown); 1/3 replicas ready.",
        signals: ["image_pull_error", "replicas_unavailable", "recent_rollout"],
        evidence: [
          ev(
            "ev-s4-k8s-1",
            "k8s_event",
            "kubernetes",
            "Failed to pull image aiops/user-service:v3.2.0: manifest unknown (ImagePullBackOff), 2 pods",
            {
              ts: at("2026-09-24T16:21:30Z"),
              link: L.grafanaK8s("user-service", ws, we),
              data: {
                reason: "ImagePullBackOff",
                object: "pod/user-service-7f9c6d5b8-k2m4q",
                type: "Warning",
                count: 27,
                image: "aiops/user-service:v3.2.0",
              },
            },
          ),
          ev(
            "ev-s4-k8s-2",
            "k8s_event",
            "kubernetes",
            "Deployment user-service revision 6 (v3.2.0) at 16:21Z; replicas 1/3 available",
            {
              ts: rollout,
              data: {
                reason: "RolloutRevision",
                object: "deployment/user-service",
                revision: "6",
                change_cause: "v3.2.0",
                replicas: { desired: 3, available: 1 },
              },
            },
          ),
        ],
        findings: [
          {
            type: "image_pull_error",
            kind: "FACT",
            description: "ImagePullBackOff for aiops/user-service:v3.2.0 (manifest unknown).",
            evidence_ids: ["ev-s4-k8s-1"],
            confidence: 0.99,
          },
          {
            type: "replicas_unavailable",
            kind: "FACT",
            description: "Only 1 of 3 replicas is ready since 16:21Z.",
            evidence_ids: ["ev-s4-k8s-2"],
            confidence: 0.98,
          },
        ],
      },
      {
        agent: "code",
        objective: "Find recent changes to user-service",
        start: 1600,
        duration: 3800,
        tools: [
          ["list_releases", 160],
          ["search_commits", 230],
          ["get_diff", 190],
        ],
        status: "success",
        summary:
          "The deployment manifest points to image tag v3.2.0, but no user-service/v3.2.0 release exists (latest is v3.1.4).",
        signals: ["image_tag_change", "unreleased_image_tag", "recent_deployment_change"],
        evidence: [
          ev(
            "ev-s4-code-1",
            "commit",
            "git",
            "Commit a91e7c3 'bump user-service to v3.2.0' references an unreleased tag",
            {
              ts: at("2026-09-24T16:15:00Z"),
              data: {
                sha: "a91e7c3d5f2b4a6c8e0d1f3b5a7c9e1d3f5b7a9c",
                short_sha: "a91e7c3d5f",
                author: "Sam Okafor",
                subject: "bump user-service to v3.2.0",
                risky: true,
                files: [
                  {
                    path: "services/user-service/k8s/deployment.yaml",
                    status: "modified",
                    hunk: "@@ -17,7 +17,7 @@ spec:\n     spec:\n       containers:\n         - name: app\n-          image: aiops/user-service:v3.1.4\n+          image: aiops/user-service:v3.2.0\n           ports:\n             - containerPort: 8000",
                  },
                ],
              },
            },
          ),
        ],
        findings: [
          {
            type: "unreleased_image_tag",
            kind: "FACT",
            description: "v3.2.0 has no release tag; the image was never built.",
            evidence_ids: ["ev-s4-code-1"],
            confidence: 0.95,
          },
        ],
      },
      {
        agent: "tickets",
        objective: "Find known issues for user-service",
        start: 1650,
        duration: 2000,
        tools: [["jira_search", 300]],
        status: "no_signal",
        summary: "No related open tickets.",
        signals: [],
        evidence: [],
        findings: [],
      },
      {
        agent: "knowledge",
        objective: "Search runbooks for rollout failures",
        start: 1700,
        duration: 3100,
        tools: [
          ["search", 240],
          ["get_doc", 150],
        ],
        status: "success",
        summary: "Runbook knowledge-base/runbooks/bad-deployment-rollback.md matches.",
        signals: ["runbook_found", "known_issue_documented", "mitigation_available"],
        evidence: [
          ev(
            "ev-s4-knowledge-1",
            "doc",
            "knowledge-base",
            "Runbook 'Bad deployment and rollback': kubectl rollout undo",
            {
              link: L.runbook("bad-deployment-rollback.md", "rollback"),
              data: {
                path: "knowledge-base/runbooks/bad-deployment-rollback.md",
                title: "Bad deployment and rollback",
                section: "Rollback",
                rank: 1,
                score: 0.91,
                excerpt:
                  "ImagePullBackOff after a rollout: roll back immediately with kubectl rollout undo, then fix the tag in git.",
              },
            },
          ),
        ],
        findings: [
          {
            type: "runbook_found",
            kind: "FACT",
            description: "bad-deployment-rollback.md is the top match.",
            evidence_ids: ["ev-s4-knowledge-1"],
            confidence: 0.91,
          },
        ],
      },
    ],
    hypotheses: [
      {
        id: "hy-s4-1",
        statement:
          "The user-service deployment references a nonexistent image tag (v3.2.0); new pods are stuck in ImagePullBackOff, leaving 1/3 replicas to serve all traffic.",
        confidence: 0.9,
        supporting_evidence_ids: [
          "ev-s4-k8s-1",
          "ev-s4-k8s-2",
          "ev-s4-code-1",
          "ev-s4-alerts-1",
          "ev-s4-logs-1",
        ],
        contradicting_evidence_ids: [],
      },
      {
        id: "hy-s4-2",
        statement: "A code regression in user-service slows logins.",
        confidence: 0.07,
        supporting_evidence_ids: ["ev-s4-metrics-1"],
        contradicting_evidence_ids: ["ev-s4-code-1"],
      },
    ],
    recommendations: [
      {
        id: "rec-s4-1",
        action: "Roll back user-service: kubectl rollout undo deployment/user-service -n prod",
        rationale: "Restores v3.1.4 with 3 ready replicas.",
        risk: "medium",
        requires_approval: true,
        evidence_ids: ["ev-s4-k8s-2", "ev-s4-knowledge-1"],
      },
      {
        id: "rec-s4-2",
        action: "Fix the image tag in git (or publish v3.2.0) before redeploying",
        rationale: "Prevents a repeat.",
        risk: "low",
        requires_approval: false,
        evidence_ids: ["ev-s4-code-1"],
      },
    ],
    timeline: [
      {
        timestamp: "2026-09-24T16:15:00Z",
        description: "Commit bumps image to v3.2.0 (unreleased)",
        source: "code",
        evidence_id: "ev-s4-code-1",
      },
      {
        timestamp: "2026-09-24T16:21:00Z",
        description: "Rollout of revision 6 starts",
        source: "k8s",
        evidence_id: "ev-s4-k8s-2",
      },
      {
        timestamp: "2026-09-24T16:21:30Z",
        description: "ImagePullBackOff on new pods",
        source: "k8s",
        evidence_id: "ev-s4-k8s-1",
      },
      {
        timestamp: "2026-09-24T16:23:40Z",
        description: "Queue pressure and 503s from callers",
        source: "logs",
        evidence_id: "ev-s4-logs-1",
      },
      {
        timestamp: "2026-09-24T16:31:00Z",
        description: "DeploymentReplicasMismatch fires",
        source: "alerts",
        evidence_id: "ev-s4-alerts-1",
      },
    ],
    report: {
      summary:
        "Logins fail because user-service runs on 1 of 3 replicas. The rollout to image tag v3.2.0 cannot pull the image (the tag was never released), so the new pods are stuck in ImagePullBackOff and callers get 503 'no healthy upstream'.",
      root_cause_hypothesis_id: "hy-s4-1",
      confidence: 0.9,
      impact:
        "503 on login and checkout for part of the traffic since 16:23Z; user-service at 33% capacity.",
      affected_services: ["user-service", "payment-service", "order-service"],
      severity: "high",
      next_steps: [
        "Roll back user-service (kubectl rollout undo).",
        "Fix the image tag in git before redeploying.",
      ],
      open_questions: ["Why did CI allow a manifest to reference an unbuilt image?"],
    },
  };
}

// ---------------------------------------------------------------------------------------------
// S5: cache outage (Redis down)
// ---------------------------------------------------------------------------------------------
function s5(): ScenarioSpec {
  const created = at("2026-09-27T15:05:00Z");
  const ws = created - 30 * MIN;
  const we = created;
  const down = at("2026-09-27T14:48:00Z");
  return {
    id: "S5",
    title: "Cache outage (Redis down)",
    description: "Redis is unavailable; all services fall back to the database and latency rises.",
    question: "Payments are slow in production",
    service: "payment-service",
    environment: "production",
    created,
    windowStart: ws,
    windowEnd: we,
    symptoms: ["cache_connection_errors", "latency_up"],
    rootCauseLabel: "Redis outage",
    variants: [
      "Payments are slow in production",
      "Everything is slow in prod",
      "Redis connection errors on payment-service",
    ],
    agents: [
      {
        agent: "logs",
        objective: "Find new error patterns for payment-service",
        start: 1300,
        duration: 5000,
        tools: [
          ["execute_esql", 440],
          ["execute_esql", 520],
        ],
        status: "success",
        summary:
          "312 'Redis connection error: ECONNREFUSED redis.prod.svc:6379; falling back to database' across services since 14:48Z.",
        signals: ["cache_connection_errors"],
        evidence: [
          logPattern(
            "ev-s5-logs-1",
            "312 Redis ECONNREFUSED errors with database fallback since 14:48Z",
            {
              index: "payment-prod-*",
              pattern:
                "Redis connection error: ECONNREFUSED <HOST>:<NUM>; falling back to database",
              level: "ERROR",
              count: 312,
              baseline: 0,
              first: at("2026-09-27T14:48:05Z"),
              last: at("2026-09-27T15:04:58Z"),
              service: "payment-service",
              start: ws,
              end: we,
              query: 'FROM *-prod-* | WHERE message LIKE "Redis connection error*"',
              samples: [
                "2026-09-27T14:48:05.330Z ERROR [payment-service v1.8.1] Redis connection error: ECONNREFUSED redis.prod.svc:6379; falling back to database",
              ],
            },
          ),
        ],
        findings: [
          {
            type: "cache_connection_errors",
            kind: "FACT",
            description: "312 Redis connection refused errors since 14:48Z, all with DB fallback.",
            evidence_ids: ["ev-s5-logs-1"],
            confidence: 0.97,
          },
        ],
      },
      {
        agent: "metrics",
        objective: "Check latency, errors and cache health",
        start: 1400,
        duration: 6400,
        tools: [
          ["query_range", 300],
          ["query_range", 310],
          ["query_range", 260],
        ],
        status: "success",
        summary:
          "redis_up 1 → 0 (cache DOWN) at 14:48Z; payment-service latency p95 up 0.15 s → 1.4 s; error rate flat.",
        signals: ["cache_down", "latency_up", "dependency_latency_up"],
        evidence: [
          metric("ev-s5-metrics-1", "redis_up", "Cache DOWN: redis_up dropped to 0 at 14:48Z", {
            metric: "redis_up",
            unit: "bool",
            service: "payment-service",
            start: ws,
            end: we,
            anomalyStart: down,
            fn: (t) => (t < down ? 1 : 0),
            baseline: 1,
            peak: 1,
            panel: "Redis",
            query: "min(redis_up)",
            noise: 0,
          }),
          metric(
            "ev-s5-metrics-2",
            "p95 latency",
            "payment-service p95 0.15 s → 1.4 s (all services slower)",
            {
              metric: "http_request_duration_p95",
              unit: "seconds",
              service: "payment-service",
              start: ws,
              end: we,
              anomalyStart: down,
              fn: (t) => 0.15 + 1.25 * ramp(t, down, down + 2 * MIN),
              baseline: 0.15,
              peak: 1.4,
              panel: "Latency p95",
              query: "histogram_quantile(0.95, …)",
              extra: [
                { label: "order-service", fn: (t) => 0.12 + 0.9 * ramp(t, down, down + 2 * MIN) },
              ],
            },
          ),
        ],
        findings: [
          {
            type: "cache_down",
            kind: "FACT",
            description: "redis_up = 0 since 14:48Z.",
            evidence_ids: ["ev-s5-metrics-1"],
            confidence: 0.99,
          },
          {
            type: "latency_up",
            kind: "FACT",
            description: "Latency up on every service at the same time.",
            evidence_ids: ["ev-s5-metrics-2"],
            confidence: 0.93,
          },
        ],
      },
      {
        agent: "alerts",
        objective: "List firing alerts",
        start: 1350,
        duration: 1900,
        tools: [
          ["list_alerts", 190],
          ["list_silences", 80],
        ],
        status: "success",
        summary: "RedisDown (critical) firing since 14:49Z; HighLatencyP95 on payment-service.",
        signals: ["alerts_firing", "critical_alert_firing", "dependency_alert_firing"],
        evidence: [
          ev(
            "ev-s5-alerts-1",
            "alert",
            "alertmanager",
            "RedisDown (critical) firing since 14:49Z",
            {
              ts: at("2026-09-27T14:49:00Z"),
              link: L.alertmanager("RedisDown"),
              data: {
                alertname: "RedisDown",
                severity: "critical",
                state: "active",
                starts_at: "2026-09-27T14:49:00Z",
                runbook_url: "knowledge-base/runbooks/redis-outage.md",
              },
            },
          ),
        ],
        findings: [
          {
            type: "dependency_alert_firing",
            kind: "FACT",
            description: "RedisDown is firing.",
            evidence_ids: ["ev-s5-alerts-1"],
            confidence: 0.99,
          },
        ],
      },
      {
        agent: "k8s",
        objective: "Check workloads and dependencies",
        start: 1500,
        duration: 3700,
        tools: [
          ["list_deployments", 220],
          ["list_events", 250],
        ],
        status: "success",
        summary: "Deployment redis scaled to 0 replicas at 14:48Z; the services' pods are healthy.",
        signals: ["dependency_unavailable"],
        evidence: [
          ev(
            "ev-s5-k8s-1",
            "k8s_event",
            "kubernetes",
            "ScalingReplicaSet: deployment redis scaled to 0 at 14:48Z",
            {
              ts: down,
              link: L.grafanaK8s("redis", ws, we),
              data: {
                reason: "ScalingReplicaSet",
                object: "deployment/redis",
                replicas: { desired: 0, available: 0 },
              },
            },
          ),
        ],
        findings: [
          {
            type: "dependency_unavailable",
            kind: "FACT",
            description: "redis is scaled to 0 replicas.",
            evidence_ids: ["ev-s5-k8s-1"],
            confidence: 0.98,
          },
        ],
      },
      {
        agent: "code",
        objective: "Find recent changes",
        start: 1600,
        duration: 2600,
        tools: [
          ["list_releases", 150],
          ["search_commits", 220],
        ],
        status: "no_signal",
        summary: "No recent code or config changes.",
        signals: ["no_recent_changes"],
        evidence: [],
        findings: [],
      },
      {
        agent: "tickets",
        objective: "Find similar incidents",
        start: 1650,
        duration: 2200,
        tools: [["jira_search", 300]],
        status: "success",
        summary: "OPS-7 'Redis eviction storm' (resolved) is a similar past incident.",
        signals: ["similar_past_incident"],
        evidence: [
          ev(
            "ev-s5-tickets-1",
            "ticket",
            "jira",
            "Resolved OPS-7 'Redis eviction storm slows all services'",
            {
              ts: at("2026-08-30T12:00:00Z"),
              link: L.ticket("OPS-7"),
              data: {
                key: "OPS-7",
                status: "Done",
                priority: "High",
                summary: "Redis eviction storm slows all services",
              },
            },
          ),
        ],
        findings: [
          {
            type: "similar_past_incident",
            kind: "CORRELATION",
            description: "OPS-7 had the same all-services slowdown when redis was unavailable.",
            evidence_ids: ["ev-s5-tickets-1"],
            confidence: 0.7,
          },
        ],
      },
      {
        agent: "knowledge",
        objective: "Search runbooks for cache failures",
        start: 1700,
        duration: 3000,
        tools: [
          ["search", 250],
          ["get_doc", 140],
        ],
        status: "success",
        summary: "Runbook knowledge-base/runbooks/redis-outage.md matches.",
        signals: ["runbook_found", "known_issue_documented", "mitigation_available"],
        evidence: [
          ev(
            "ev-s5-knowledge-1",
            "doc",
            "knowledge-base",
            "Runbook 'Redis outage': scale redis back up, watch DB load",
            {
              link: L.runbook("redis-outage.md", "mitigation"),
              data: {
                path: "knowledge-base/runbooks/redis-outage.md",
                title: "Redis outage",
                section: "Mitigation",
                rank: 1,
                score: 0.93,
                excerpt:
                  "kubectl scale deployment/redis --replicas=1 -n prod; watch Postgres CPU while caches warm up.",
              },
            },
          ),
        ],
        findings: [
          {
            type: "runbook_found",
            kind: "FACT",
            description: "redis-outage.md is the top match.",
            evidence_ids: ["ev-s5-knowledge-1"],
            confidence: 0.93,
          },
        ],
      },
    ],
    hypotheses: [
      {
        id: "hy-s5-1",
        statement:
          "Redis outage (deployment scaled to 0) forces every service to fall back to the database, increasing latency across services.",
        confidence: 0.89,
        supporting_evidence_ids: [
          "ev-s5-metrics-1",
          "ev-s5-k8s-1",
          "ev-s5-logs-1",
          "ev-s5-alerts-1",
          "ev-s5-metrics-2",
        ],
        contradicting_evidence_ids: [],
      },
    ],
    recommendations: [
      {
        id: "rec-s5-1",
        action: "Scale redis back up: kubectl scale deployment/redis --replicas=1 -n prod",
        rationale: "Restores the cache.",
        risk: "low",
        requires_approval: true,
        evidence_ids: ["ev-s5-k8s-1", "ev-s5-knowledge-1"],
      },
      {
        id: "rec-s5-2",
        action: "Watch Postgres CPU while caches warm up",
        rationale: "Cold caches increase DB load.",
        risk: "low",
        requires_approval: false,
        evidence_ids: ["ev-s5-knowledge-1"],
      },
    ],
    timeline: [
      {
        timestamp: "2026-09-27T14:48:00Z",
        description: "redis scaled to 0 replicas",
        source: "k8s",
        evidence_id: "ev-s5-k8s-1",
      },
      {
        timestamp: "2026-09-27T14:48:05Z",
        description: "First ECONNREFUSED; services fall back to the DB",
        source: "logs",
        evidence_id: "ev-s5-logs-1",
      },
      {
        timestamp: "2026-09-27T14:49:00Z",
        description: "RedisDown fires",
        source: "alerts",
        evidence_id: "ev-s5-alerts-1",
      },
      {
        timestamp: "2026-09-27T14:50:00Z",
        description: "Latency up on all services",
        source: "metrics",
        evidence_id: "ev-s5-metrics-2",
      },
    ],
    report: {
      summary:
        "Payments are slow because Redis is down: the redis deployment was scaled to 0 at 14:48Z. Every service falls back to the database, so latency rose about 9× across services, although requests still succeed.",
      root_cause_hypothesis_id: "hy-s5-1",
      confidence: 0.89,
      impact:
        "p95 latency 1.4 s (≈9×) on payment-service and higher latency on all services since 14:48Z; no errors.",
      affected_services: ["payment-service", "order-service", "user-service", "inventory-service"],
      severity: "high",
      next_steps: [
        "Scale redis back to 1 replica.",
        "Find out who scaled it down (audit log) and why.",
      ],
      open_questions: ["Who scaled redis to 0?"],
    },
  };
}

export const SCENARIOS: ScenarioSpec[] = [s0(), s1(), s2(), s3(), s4(), s5()];
