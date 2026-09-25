# Troubleshooting

## `ModuleNotFoundError: No module named 'aiops'` after `uv sync` (macOS)
If the repository sits in an iCloud-synced folder (Desktop/Documents), macOS marks new files as `hidden`. Python 3.12+ **ignores hidden `.pth` files**, so the editable install disappears.

- Every `make` target runs `make venv-fix` first, which syncs and clears the flag. If you run `uv run aiops` directly after a dependency change, run `make venv-fix` first.
- Root cause: `~/Desktop` is an iCloud File Provider folder. The permanent fix is to keep the repo outside iCloud-synced folders, e.g. `~/code/AI_Incident_Agents`.
- The tests are unaffected: pytest adds `src` to `pythonpath`.

## Ports already in use
`make preflight` lists busy ports. Stop the conflicting local services (e.g. `brew services stop postgresql redis`), or change the host ports in `.env` once the infra PRs add them.
