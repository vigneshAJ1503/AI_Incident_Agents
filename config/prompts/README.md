# Prompts

Versioned prompts live at `config/prompts/<name>/v<N>.md`. The highest version is used unless it's pinned in the environment config (`agents.<name>.prompt_version`).

- **Never edit a released version in place.** Copy it to `v<N+1>.md` and run the evals to compare.
- Every run records `name/version@sha`.
- Variables use `$name` syntax (Python `string.Template`); a missing variable is an error.
- `common/` holds the safety preamble that is prepended to every agent's system prompt.
- `providers/<capability>/<provider>/v<N>.md` are **provider prompt fragments** (ADR-0012): the vendor's tool names and query-language examples. The selected provider's fragment is rendered with the agent's variables into `$provider_guidance`, so agent prompts stay vendor-neutral. The run records both refs, e.g. `logs/v3@<sha>+providers/logs/elasticsearch/v1@<sha>`.
