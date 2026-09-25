# Incident scenarios

Each scenario is a reproducible incident with **ground truth**, used for agent tests and evals (MASTER_PLAN.md §6, §15).

```
scenarios/<id>/
├── README.md            # what happens, what the investigation should conclude
├── expected.yaml        # question, service, window, ground-truth root cause
└── agents/<agent>.yaml  # expectations for one agent (status, signals, ...)
```

- **Synthetic data:** PR-007 seeds log data for each scenario.
- **Live data:** PR-017 adds live fault injection (`inject.sh` / `revert.sh`).

`expected.yaml` fields:

| Field | Meaning |
|-------|---------|
| `question` | What the engineer asks |
| `service`, `environment`, `window` | The context the planner should extract |
| `root_cause` | Ground-truth root cause (`null` = healthy; agents must not invent one) |
| `agents/<name>.yaml` → `status` | Acceptable agent statuses |
| `signals` | Signals that must be reported |
| `forbidden_signals` | Signals that must NOT be reported |
| `must_mention` | Phrases the summary or findings must contain (case-insensitive) |
| `min_evidence` | Minimum number of evidence items |
