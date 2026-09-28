# ADR-0011: Company profile pack (one folder per company, selected by AIOPS_PROFILE)

- **Status:** Accepted
- **Date:** 2026-09-28

## Context
The project's top goal is portability: when it moves to a new company, only configuration
changes, never code. Until now one company's setup was spread over
`config/environments/<env>.yaml` (LLM, capabilities, limits), `config/service-catalog/<env>.yaml`
(a name linked by `service_catalog:`), the shared `config/prompts/`, and variables listed in
a repo-wide `.env.example` next to the docker-compose ports. Onboarding meant knowing which
four places to edit, nothing validated that a provider was usable, and a public repository
invited committing a company's configuration next to the demo.

## Decision
- **A profile = one self-contained folder** `profiles/<name>/`:
  `profile.yaml` (what environment files held, plus `metadata: {company, description, owners}`
  and `extends:`), `services.yaml` (the service catalog), `.env.example` (every `${VAR}` it
  uses) and optional `prompts/<agent>/vN.md` overrides that fall back to the shared
  `config/prompts/`. The folder name is the profile name; the catalog is found next to the
  profile (or inherited from the nearest `extends` ancestor that has one).
- **Selection:** `--profile` (alias `--env`) > `AIOPS_PROFILE` > `AIOPS_ENV` (deprecated
  alias, one-line notice) > `local`. `AIOPS_PROFILES_DIR` moves the whole folder outside
  the repository.
- **Back-compat:** a legacy `config/environments/<name>.yaml` (+ `config/service-catalog/`)
  still loads when no profile of that name exists; the repo's `local` and `local-k8s`
  moved to `profiles/` unchanged in content. `Settings`, `load_settings()`, the catalog and
  prompt APIs keep their signatures, so agents, tests, fixtures and evals are untouched.
- **Semantic validation** (`aiops profile validate`) on top of the strict Pydantic schema: a
  provider matrix in code (`core/profiles.py: PROVIDERS`, status implemented/planned,
  required settings, the tools each agent calls) produces readable errors such as
  `capability logs: provider 'loki' needs setting 'labels.service'`, plus catalog and
  `.env.example` coverage checks.
- **Tooling:** `aiops profile list | show [--resolved] | validate | init | diff`. `init`
  copies `_template` (heavily commented) or another profile, sets metadata, never copies a
  `.env`, and prints the next steps.
- **Secrets:** only `${VAR}` references in YAML; values from the shell > `profiles/<name>/.env`
  > the repo `.env`. Displays mask secret-looking keys. `profiles/*` is gitignored except the
  shipped `local`, `local-k8s` and `_template`.

## Alternatives considered
- **Keep `config/environments/` and add a `company:` key.** Still four places to edit and no
  natural home for prompts and secrets per company.
- **A single file per company** (catalog inline). Catalogs grow to hundreds of services and
  are often generated (Backstage/CMDB); a separate `services.yaml` can be regenerated
  without touching hand-written settings.
- **Validate providers inside `load_settings`.** Would make every load (tests, replay evals)
  depend on the provider registry and fail on planned providers during experiments; kept as
  an explicit `validate` step instead (and `aiops doctor`, PR-P3, which also checks connectivity, tool contracts and catalog identifiers live).

## Consequences
- Onboarding = `aiops profile init <company>`, edit two YAML files, fill `.env`, `validate`.
- Vendor logic that is still in code (ES|QL, JQL, Alertmanager matchers, tool names) is
  listed in docs/portability.md with file:line references; PR-P2 moves it behind provider
  adapters selected by `provider`, so the "planned" rows of the matrix become config-only.
- Other open PRs that edit `config/environments/local.yaml` or `config/service-catalog/`
  must rebase onto `profiles/local/` (git's rename detection usually carries the change).
