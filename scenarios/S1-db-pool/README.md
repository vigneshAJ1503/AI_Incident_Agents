# S1 — DB connection pool misconfiguration

The design's reference incident. At T-20m, payment-service `v1.8.2` rolls out with `DB_POOL_SIZE=2` (it was 20 in `v1.8.1`).

**Signals by source:**
- **Logs:** a new `Database connection timeout` pattern on `/api/v1/pay`, HTTP 500s, first seen right after the v1.8.2 rollout.
- **Metrics (later):** p95 latency ≥ 5×, 5xx rate from ~0.5% to ~25%, DB pool saturation at 100%.
- **Alerts:** `DatabaseConnectionPoolExhausted` (T-19m) then `HighErrorRate` (T-18m), both critical (`make seed-alerts S=S1`).
- **K8s / Code (later):** the v1.8.2 rollout and a `tune db pool` commit that changed `DB_POOL_SIZE`.

**Expected conclusion:** a pool misconfiguration in v1.8.2 (hypothesis, confidence ≥ 0.8).
