# Prompts

Versioned prompts live at `config/prompts/<name>/v<N>.md`. The highest version is used unless it's pinned in the environment config (`agents.<name>.prompt_version`).

- **Never edit a released version in place.** Copy it to `v<N+1>.md` and run the evals to compare.
- Every run records `name/version@sha`.
- Variables use `$name` syntax (Python `string.Template`); a missing variable is an error.
- `common/` holds the safety preamble that is prepended to every agent's system prompt.
