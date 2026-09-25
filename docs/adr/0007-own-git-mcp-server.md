# ADR-0007: Our own read-only Git MCP server

- **Status:** Accepted
- **Date:** 2026-09-26

## Context
The Code agent (UC-08, MASTER_PLAN D7) needs to list the commits that touched a service's paths in a time window, read their diffs and see the release tags, all against a local repository (`.data/sample-repo`, PR-026) and, in a company, GitHub or GitLab.

The official reference server `mcp-server-git` (Docker image `mcp/git`) exposes:
- `git_status`, `git_diff_unstaged`, `git_diff_staged`, `git_diff`, `git_log`, `git_show`, `git_branch`
- **write tools:** `git_commit`, `git_add`, `git_reset`, `git_create_branch`, `git_checkout`

`git_log` accepts `max_count` and `start_timestamp`/`end_timestamp`, but has **no path filter**, and there is **no tag or release listing**. Every tool takes a `repo_path` file system path from the client. Read-only access would depend only on our client-side allowlist, and the server has no output caps.

## Decision
Build `mcp-servers/git-mcp` (MCP SDK v2 `MCPServer`), a thin server with five read-only tools:
- `list_repositories`
- `list_releases(repo, prefix?)`
- `search_commits(repo, since, until, paths?, grep?, limit)`
- `get_commit(repo, sha)`
- `get_diff(repo, sha | base+head, paths?, context_lines)`

The guardrails are enforced **server-side**:
- **Repo allowlist** by logical name (`GIT_REPOS=name=/path`). Clients never send file system paths to a repository.
- **Path validation:** relative paths only. It rejects `..`, `.git`, absolute paths, pathspec magic and globs, and pathspecs are literal.
- **Ref validation:** SHAs or tag/branch names only, resolved with `rev-parse --verify --end-of-options`.
- **Read-only git:** only `log`, `show`, `diff`, `rev-parse` and `for-each-ref`, run with a fixed argv and no shell. User and system config are ignored. Hooks, fsmonitor, pager, external diff and textconv are off. Optional locks are off.
- **Limits:** maximum time range, commit, file and diff-size caps (whole hunks, with a `truncated` flag), and a per-command timeout.
- **Container:** non-root, repository mounted **read-only**, root filesystem read-only, bound to `127.0.0.1:8107`.

The agent binds to the `code` capability, not to this server. Repo names and paths come from the service catalog (`code: {repo, paths}`), and the release tag scheme and optional commit link template come from `capabilities.code.settings`.

## Consequences
- The tools map one-to-one onto the Code agent's deterministic phase (a time window plus paths, then diffs of the suspects, then release tags), so we don't need to parse `git log` text in the backend.
- Any client gets the same protection, and there are no write tools to allowlist away.
- We maintain about 400 lines of code. For companies, the capability can point at the GitHub or GitLab MCP servers instead (`provider: github`), with a different tool allowlist and prompt version. The agent code needs an adapter for their tool names, which is planned for when that integration is needed.
