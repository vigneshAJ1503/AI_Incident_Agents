# ADR-0018: System evaluation: one scorecard, a committed baseline as the CI gate

- **Status:** Accepted
- **Date:** 2026-09-29
- **PR:** PR-040 (docs/evals.md)

## Context
Each agent had its own eval command (`aiops eval run`), and whole investigations had
another (`aiops eval investigations`). Nothing answered "is the system better or worse
than yesterday?" in one place, or kept a record of which model, prompts and code produced
a number. MASTER_PLAN §15 sets v1.0 targets: ≥ 4/5 root causes, 0 false positives on S0,
≥ 95 % valid citations and p50 < 90 s. CI has no LLM key and must spend no tokens.

## Decision
1. **One command, one report:** `aiops evaluate` runs the per-agent evals, the planner
   cases and the S0–S5 investigations end to end. It writes
   `evals/reports/<date>-system-<mode>.md` + JSON, headed by the reproducibility data:
   profile, provider, models, prompt refs (with content hashes, provider fragments
   included), agent versions and git SHA.
2. **Rule-based scoring gates; the LLM judge only informs.** Root-cause accuracy is the
   keyword rule of `expected.yaml`, which is deterministic and runs in replay. The optional
   LLM judge (prompt `judge/v1`) needs a key and is reported next to it, never faked and
   never used as a gate.
3. **The gate compares with a committed baseline** (`evals/baselines/replay.json`), not
   with fixed thresholds. The job fails when a pass rate or an accuracy drops, or a
   false-positive rate rises. Accepting new numbers is an explicit, reviewable diff
   (`make evaluate-baseline`). Known gaps are recorded honestly, e.g. the planner asks for
   clarification on S4's two-service question.
4. **Negative and ambiguous questions** are planner-level cases in
   `scenarios/planner-cases.yaml`. They need no fixtures, and the scenario loader
   (`*/expected.yaml`) ignores the file, so the demo datasets are unaffected.
5. **Cost** comes from a per-profile price table (`evals.pricing`, USD per 1M tokens: model
   id, then provider host, then `*`). It defaults to 0 for free tiers.

## Consequences
- CI job `evals` runs in seconds with zero tokens, and uploads the scorecard as an
  artifact.
- The replay scorecard measures the deterministic pipeline only. LLM reasoning quality
  needs `make evaluate-live` (local, with a key); that run can keep its own
  `evals/baselines/live.json`.
- The Brier score in replay is dominated by the rule-based confidences. It becomes
  informative in live runs, where the LLM ranks the hypotheses.
