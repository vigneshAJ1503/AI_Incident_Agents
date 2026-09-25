# git-mcp

A read-only, guard-railed Git MCP server for AI Incident Agents (ADR-0007). The Code agent (UC-08) uses it to find recent code and config changes.

## Tools

| Tool | Purpose |
|------|---------|
| `list_repositories` | Names of the allowlisted repositories |
| `list_releases` | Release tags, newest first (tag, commit SHA, date, message); optional `prefix` such as `payment-service/` |
| `search_commits` | Commits in `[since, until]`, optionally limited to `paths` and a literal `grep` on the message, with changed files and tags |
| `get_commit` | Metadata, full message, tags and per-file line stats |
| `get_diff` | Unified diff split into files and hunks, for one commit (`sha`) or a range (`base`..`head`, e.g. two release tags) |

## Guardrails (enforced server-side, for every client)
- **Repo allowlist:** only repositories named in `GIT_REPOS` are readable, by logical name. Clients never pass file system paths to a repository.
- **Paths:** relative to the repo root. Absolute paths, `..`, `.git`, a leading `-` or `:` (pathspec magic) and glob characters are rejected. Pathspecs are literal (`GIT_LITERAL_PATHSPECS=1`).
- **Refs:** plain SHAs or tag/branch names only (no `..`, `~`, `^`, `@{`, or leading `-`). They're resolved with `rev-parse --verify --end-of-options`.
- **Read-only git:** a fixed argv and no shell. Only `log`, `show`, `diff`, `rev-parse` and `for-each-ref` can run. User and system git config are ignored. Hooks, fsmonitor, pager, external diff and textconv are disabled, so a hostile `.git/config` can't execute programs. Optional locks are off, so nothing is written to the repo.
- **Limits:** time range ≤ `MAX_RANGE_DAYS`, ≤ `MAX_COMMITS` commits, diff output ≤ `MAX_DIFF_CHARS` (whole hunks only; `truncated: true` when anything is cut), ≤ `MAX_FILES` files, and a per-command timeout.
- **Deployment:** runs as a non-root container, with the repository mounted **read-only**.

## Configuration (env)

| Variable | Default |
|----------|---------|
| `GIT_REPOS` | `sample-repo=/repos/sample-repo` (comma-separated `name=/abs/path`) |
| `MAX_COMMITS` | `200` |
| `MAX_DIFF_CHARS` | `40000` |
| `MAX_FILES` | `200` |
| `MAX_RANGE_DAYS` | `90` |
| `COMMAND_TIMEOUT_S` | `15` |

## Run
```bash
make seed-repo S=S1                         # builds .data/sample-repo
GIT_REPOS=sample-repo=$PWD/../../.data/sample-repo uv run git-mcp --transport http --port 8107
# or in Docker (mounts .data/sample-repo read-only):
docker compose --env-file .env.example -f deploy/compose/docker-compose.mcp.yml up -d --build git-mcp
```
