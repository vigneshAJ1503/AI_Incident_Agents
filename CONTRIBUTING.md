# Contributing

Development follows the PR-by-PR roadmap in [MASTER_PLAN.md §14](MASTER_PLAN.md#14-pr-by-pr-roadmap).

## Workflow
1. Pick the next PR from §14 and open (or find) its issue.
2. Branch from `main`: `feat/NNN-short-name` (also `fix/…`, `refactor/…`, `test/…`, `docs/…`).
3. Implement with tests. Run `make check` before pushing.
4. Open a PR using the template. CI must be green. Squash merge.

## Commit messages
[Conventional Commits](https://www.conventionalcommits.org/) with a scope:

```
feat(elk): add elasticsearch MCP client
fix(orchestrator): prevent duplicate investigation steps
test(elk): add query builder tests
docs(architecture): document agent boundaries
```

## Local setup
```bash
make preflight   # check prerequisites
make setup       # install deps + git hooks
make check       # lint + typecheck + tests
```

## Ground rules
- Never commit secrets. Only `.env.example` is tracked.
- Agents access external systems **only** through allowlisted MCP tools.
- Any write action (tickets, Kubernetes, …) requires human approval.
- Agents bind to capabilities (`logs`, `metrics`, …), never to a vendor, so the platform stays portable.
