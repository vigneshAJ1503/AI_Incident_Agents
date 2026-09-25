# Troubleshooting

## `ModuleNotFoundError: No module named 'aiops'` after `uv sync` (macOS)
If the repository sits in an iCloud-synced folder (Desktop/Documents), macOS marks new files as `hidden`. Python 3.12+ **ignores hidden `.pth` files**, so the editable install disappears.

- `make setup` clears the flag (`chflags -R nohidden backend/.venv`). Run it again after any `uv sync`.
- The permanent fix is to keep the repo outside iCloud-synced folders, e.g. `~/code/AI_Incident_Agents`.
- The tests are unaffected: pytest adds `src` to `pythonpath`.

## Ports already in use
`make preflight` lists busy ports. Stop the conflicting local services (e.g. `brew services stop postgresql redis`), or change the host ports in `.env` once the infra PRs add them.
