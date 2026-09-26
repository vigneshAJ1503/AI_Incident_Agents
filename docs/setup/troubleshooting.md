# Troubleshooting

## `ModuleNotFoundError: No module named 'aiops'` after `uv sync` (macOS)
If the repository sits in an iCloud-synced folder (Desktop/Documents), macOS marks new files as `hidden`. Python 3.12+ **ignores hidden `.pth` files**, so the editable install disappears.

- Every `make` target runs `make venv-fix` first, which syncs and clears the flag. If you run `uv run aiops` directly after a dependency change, run `make venv-fix` first.
- Root cause: `~/Desktop` is an iCloud File Provider folder. The permanent fix is to keep the repo outside iCloud-synced folders, e.g. `~/code/AI_Incident_Agents`.
- The tests are unaffected: pytest adds `src` to `pythonpath`.

## Ports already in use
`make preflight` lists busy ports. Stop the conflicting local services (e.g. `brew services stop postgresql redis`), or change the host ports in `.env` once the infra PRs add them.

## Minikube won't start after `make infra-down` ("network ... not found")
Minikube's node container is attached to the Docker network `aiops`. The network is now created **outside** compose by `scripts/ensure-network.sh`, with a pinned subnet `172.21.0.0/16`, and compose treats it as `external`, so `infra-down` never removes it and Minikube's static IP `172.21.0.100` stays valid.

If you still have an old compose-managed network (created before this change), migrate it once:
```bash
make mcp-down infra-down k8s-down
docker network rm aiops && minikube delete -p aiops
make infra-up mcp-up k8s-up
```
The cluster is stateless (manifests re-apply, sample data is ephemeral), so recreating it is safe.
