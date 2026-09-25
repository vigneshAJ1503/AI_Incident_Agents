"""Versioned prompts: config/prompts/<name>/v<N>.md.

Every investigation records which prompt version (and content hash) produced it,
so results are reproducible and prompt changes can be evaluated.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from string import Template

from pydantic import BaseModel, ConfigDict

from aiops.core.config import ConfigError

_VERSION_FILE = re.compile(r"^v(\d+)\.md$")


class Prompt(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: str  # "v1"
    text: str
    sha: str  # first 12 hex chars of sha256(text)

    @property
    def ref(self) -> str:
        return f"{self.name}/{self.version}@{self.sha}"

    def render(self, **variables: object) -> str:
        """Substitute $variables. Missing variables are an error (no silent blanks)."""
        try:
            return Template(self.text).substitute({k: str(v) for k, v in variables.items()})
        except KeyError as exc:
            raise ConfigError(f"Prompt {self.ref} needs variable {exc}") from exc


class PromptLoader:
    def __init__(self, prompts_dir: Path) -> None:
        self._dir = prompts_dir

    def versions(self, name: str) -> list[str]:
        folder = self._dir / name
        found = []
        for path in folder.glob("v*.md") if folder.is_dir() else []:
            match = _VERSION_FILE.match(path.name)
            if match:
                found.append(int(match.group(1)))
        return [f"v{n}" for n in sorted(found)]

    def load(self, name: str, version: str | None = None) -> Prompt:
        available = self.versions(name)
        if not available:
            raise ConfigError(f"No prompts found for '{name}' in {self._dir / name}")
        chosen = version or available[-1]
        if chosen not in available:
            raise ConfigError(f"Prompt '{name}' has no version {chosen} (available: {available})")
        text = (self._dir / name / f"{chosen}.md").read_text()
        sha = hashlib.sha256(text.encode()).hexdigest()[:12]
        return Prompt(name=name, version=chosen, text=text, sha=sha)

    def names(self) -> list[str]:
        return sorted(p.name for p in self._dir.iterdir() if p.is_dir() and self.versions(p.name))
