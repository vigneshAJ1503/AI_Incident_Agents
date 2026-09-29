# Evaluating the system (PR-040)

One command scores the whole system and writes one scorecard:

```bash
make evaluate                    # replay: recorded fixtures, zero tokens, ~5 s, + regression gate
make evaluate-live [JUDGE=1]     # live stack + the configured hosted LLM (spends tokens)
```

Both call `aiops evaluate` (from `backend/`):

```
aiops evaluate [--agents all|logs,metrics,...|none] [--investigations/--no-investigations]
               [--mode replay|live] [--profile X] [--scenario S1 ...] [--judge]
               [--baseline PATH] [--no-gate] [--update-baseline] [--out DIR] [--no-write]
```

- With no options, it runs everything: every agent, the planner cases and S0–S5 end to end.
- `--agents logs,metrics` runs only those agents. Add `--investigations` to include the
  end-to-end part too.
- `--investigations` on its own runs only the planner cases and the end-to-end
  investigations.
- `--scenario S1` limits every part to the given scenarios. The regression gate is skipped
  then, because the rates of a subset can't be compared with the baseline.
- `--profile` picks the company profile (`AIOPS_PROFILE`). Every number in the report
  depends on it.

The single-agent runner (`make eval AGENT=logs`, `aiops eval run`) and `aiops eval
investigations` still exist for quick loops on one part.

## What is measured

| Part | Where the ground truth lives | Metrics |
|------|------------------------------|---------|
| **Agents**: each agent on its own, per scenario | `scenarios/<id>/agents/<agent>.yaml` | pass rate, false-positive rate (healthy S0), citation validity (findings cite existing evidence), avg tool calls, tokens, p50 latency, cost |
| **Planner**: service identification from the question alone | the scenario questions + `scenarios/planner-cases.yaml` | service accuracy, clarification accuracy (vague or ambiguous questions must be clarified, not guessed), **invented services** (must stay 0) |
| **Investigations**: S0–S5 end to end (planner, 7 agents in 2 rounds, RCA, report) | `scenarios/<id>/expected.yaml` (`root_cause`, `investigation:`) | root-cause accuracy (rule-based; optional LLM judge), false positives, confidence calibration (Brier score), claim citations, severity, time to report, tokens, cost |

- **Root-cause accuracy, rule-based.** The top hypothesis must mention one word from every
  group in `investigation.root_cause_keywords`. On a healthy scenario it must report no
  hypothesis at all. This check is deterministic and gates CI.
- **LLM judge (optional).** `--judge` (or `evals.llm_judge: true`) asks the configured
  hosted model (`fast` role, prompt `config/prompts/judge/v1.md`) whether the reported root
  cause is the same cause as the ground-truth `root_cause` text. It tolerates paraphrases
  that the keyword rule would miss. Without a key it is **skipped with a note in the
  report**. It is never faked, and it never gates.
- **Brier score.** This is the mean of (confidence − outcome)² over all scenarios. The
  outcome is 1 when the reported root cause is correct, and 0 otherwise. A healthy scenario
  has no correct root cause, so any confidence there counts against calibration. 0 is
  perfect; a system that always says 50% scores 0.25.
- **Cost.** The profile has a price table, `cost.pricing` (PR-041; the older `evals.pricing`
  is still read), in USD per 1M tokens. A price is looked up by model id first, then by
  provider host (e.g. `api.groq.com`), then `*`. The default is 0, which is right for the
  free tiers this project uses. The same table prices every live LLM call, so cost per agent
  and per investigation also shows in the API, the Web UI and `/metrics`
  ([observability.md](observability.md)).

## Reading the scorecard

The report goes to `evals/reports/<date>-system-<mode>.md`, with a JSON twin for tooling:

1. **Run.** This section is what you need to reproduce the numbers or explain a change:
   profile, configured LLM provider and models, the LLM that actually answered (replay
   uses a scripted one), git SHA (flagged when the tree had uncommitted changes), and
   whether the judge ran.
2. **Regression gate.** It shows PASS, or one line per metric that got worse than the
   baseline.
3. **Investigations.** The headline table carries the MASTER_PLAN §15 v1.0 targets
   (≥ 4/5 root causes, 0 false positives, ≥ 95 % valid citations, p50 < 90 s), followed by
   one row per scenario. Every failed check is listed with its detail.
4. **Planner.** Each question, what was expected and what the planner did.
5. **Agents.** One row per agent, with the failed checks per scenario.
6. **Versions.** Agent versions and prompt refs. A ref such as
   `logs/v3@80c6de519237+providers/logs/elasticsearch/v1@a5776d7915e3` names the agent
   prompt and the provider fragment, each with a content hash. A prompt edit therefore
   shows up as a new hash, even when the version number stays the same.

Keep in mind:

- **Replay numbers score the deterministic pipeline**: planning, agent queries and
  signals, the rule-based RCA, guardrails and the report. They do not score LLM
  reasoning, and replay times are not live latency.
- **Live numbers vary between runs**, so compare several.
- The planner row for **S4** ("Login and checkout requests are failing") asks for
  clarification (user-service or order-service?). The question really does name two
  services. The baseline records this, so a fix raises the number and a new miss fails the
  gate.

## The regression gate (CI job `evals`)

CI runs `aiops evaluate --mode replay` on every PR. It uses no tokens and no services. The
job fails (exit 1) when, compared with `evals/baselines/replay.json`:

- any **pass rate** drops: per agent, investigations or planner;
- **root-cause accuracy**, **service accuracy** or **clarification accuracy** drops;
- any **false-positive rate** rises: per agent or investigations.

Only what both runs measured is compared, so `--agents logs` is not failed by the other
agents. `evals.tolerance` (default 0) allows a small move, which is useful for live
baselines. The scorecard is uploaded as the CI artifact `system-scorecard-replay`.

**When a change improves the numbers**, or is meant to change them (for example a new
scenario), accept the new numbers explicitly:

```bash
make evaluate-baseline      # rewrites evals/baselines/replay.json; commit it with the change
```

The diff of that file is the reviewable record of the change in quality. For a live
baseline, run `aiops evaluate --mode live --update-baseline`; it writes
`evals/baselines/live.json`. That run is local only, because it needs the live stack and a
key.

## Adding a scenario

1. Create `scenarios/S6-<name>/expected.yaml` with `id`, `title`, `question`, `service`,
   `window` and `root_cause` (use `null` for a healthy scenario). Add the `investigation:`
   ground truth: `root_cause_keywords` (groups of alternatives), `min_confidence` and
   `severity` (see `orchestrator.severity` in the profile).
2. For each agent, add `scenarios/S6-<name>/agents/<agent>.yaml` with the status, the
   signals that must and must not appear, `must_mention` and `min_evidence`.
3. Record fixtures for every agent: `backend/tests/fixtures/<agent>/S6/`. You can use the
   synthetic generators (`aiops seed ...`) or record live with a fault
   (`aiops fault run S6 -- <recorder>`, which writes `meta.json` with the recorded window;
   see `scenarios/README.md`).
4. Run `make evaluate`, check the new rows, then run `make evaluate-baseline` and commit.
5. The demo datasets list S0–S5 explicitly (`test_demo_datasets.py`, the Web UI's static
   dataset), so add the scenario there too, or keep it out of the demo on purpose.

**A question without data** (a negative or ambiguous case) only needs an entry in
`scenarios/planner-cases.yaml`:

- `expect: clarification`, optionally with the `candidates` that must be offered and the
  names that must `never` be planned (unknown services);
- or `expect: service` with the `service` it must resolve to.

## Judging a prompt change

1. Run `make evaluate` before and after the change. Replay catches regressions in parsing,
   guardrails and the deterministic phase that the prompt feeds. The prompt ref in
   **Versions** shows that the new prompt was used.
2. Replay does not exercise the prompt's reasoning. For that, run `make evaluate-live
   JUDGE=1` on both versions, pinning the old one through the profile
   (`agents.<name>.prompt_version: v2`). Compare root-cause accuracy (rule and judge),
   false positives, the Brier score, tokens and cost. Run each version more than once:
   free-tier models aren't deterministic even at temperature 0.
3. Ship the change when accuracy is not lower, false positives stay at 0, and the token
   and cost increase is justified. Then commit the new report in `evals/reports/`.
