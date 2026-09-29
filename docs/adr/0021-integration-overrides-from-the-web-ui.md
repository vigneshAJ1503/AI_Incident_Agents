# ADR-0021: Integration overrides from the Web UI

- **Status:** Accepted
- **Date:** 2026-09-29

## Context
PR-046 lets someone configure integrations (the capability → provider bindings of a
company profile, ADR-0011/0012) from the Web UI instead of editing YAML, without giving up
what makes profiles safe: reviewed YAML as the source of truth, readable validation, no
secrets in git, and agents that stay read-only. It also has to work with zero extra
infrastructure (no Vault, no paid secret manager) and apply without restarting the API.

## Decision
- **An overlay, not a second source of truth.** The profile YAML keeps every default. The
  UI stores only what a person changed, per `(profile, capability)`, in the evidence store
  (Postgres, Alembic revision `0002`: `integration_override`). The API's effective settings
  = YAML + overlay. Deleting the row (or "Reset to profile") restores the YAML exactly.
- **A narrow, typed surface.** Editable: `enabled`, `provider` (implemented providers
  only), `mcp.url`, `mcp.timeout_s`, three `limits`, and every `settings.*` key the profile
  declares (also `${VAR:-}` keys that resolved empty), typed like the profile value.
  **Not** editable: tool allowlists, write allowlists and guardrail-looking settings
  (`*approval*`, `*allowlist*`, `read_only`): the agents' read-only boundary stays in
  reviewed YAML. Credential-looking settings are never shown or edited as plain fields.
- **Same validation as the YAML.** A change is applied to the capability and validated
  with `CapabilityConfig` (Pydantic) and `aiops profile validate`'s semantic checks before
  anything is stored; errors are HTTP 422 with the readable message, warnings are returned.
- **Secrets are write-only MCP headers, encrypted with a key from env.** A new
  `mcp.headers` (for MCP servers behind auth; also usable from YAML as `${VAR}`) is the one
  place a UI secret goes. Values are Fernet-encrypted (`cryptography`) with
  `AIOPS_SECRETS_KEY` and stored as ciphertext + at most the last 4 characters of long
  values. The API never returns a value. Without the key, saving a secret is refused with a
  message saying how to set it; a wrong/rotated key makes stored secrets `usable: false`
  (left out of the headers), never a crash.
- **Audit by field, never by value.** Every save writes an `integration_audit` row (actor,
  capability, `[{field, change}]`) in the same transaction; logs carry field names only.
- **Reload = swap the settings object.** After a save the API rebuilds the effective
  settings and swaps them into everything that starts later: new investigations (the
  runner builds each orchestrator from the current settings), health checks, approvals'
  executor, the chat classifier. Running investigations keep the orchestrator and settings
  they started with. Only capabilities change, so auth/CORS/limits of the API itself are
  unaffected. An override that no longer validates (e.g. the YAML changed) is skipped with a
  note instead of breaking startup.
- **Test connection reuses `aiops doctor`.** `POST /integrations/{cap}/test` runs the
  doctor's per-capability checks (config, reachability, contract, smoke, catalog) on the
  saved settings plus an optional unsaved draft, through the same guarded toolset.
- **Auth:** with `api.auth` on, save and test need an authenticated principal (the actor in
  the audit); reads stay as they are.

## Consequences
- A company can move between providers/URLs/field names from the UI; YAML still wins for
  everything reviewed (allowlists, guardrails, the catalog, the LLM).
- The overlay is per API database. Several API replicas share it through Postgres, but each
  process applies a change when *it* saves or restarts (the multi-replica reload is a
  follow-up, like the in-process rate limits of PR-042). The CLI (`aiops doctor`, `aiops
  investigate`) reads the YAML only.
- Test connection makes the API connect to a URL a caller supplies: with `api.auth: none`
  (local only, API bound to 127.0.0.1) anyone who can reach the API can do that; production
  deployments must turn auth on.
- Rotating `AIOPS_SECRETS_KEY` means typing UI secrets again (documented; a re-encrypt
  command is a follow-up).
