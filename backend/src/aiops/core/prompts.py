"""Versioned prompts: config/prompts/<name>/v<N>.md.

Every investigation records which prompt version (and content hash) produced it,
so results are reproducible and prompt changes can be evaluated.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
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
    """Prompts from ``prompts_dir``; ``overrides`` (e.g. a profile's ``prompts/``) win.

    A version file in an override folder replaces the shared one with the same name, and
    new versions there extend the list (so ``latest`` may come from the profile).
    """

    def __init__(self, prompts_dir: Path, overrides: Sequence[Path] = ()) -> None:
        self._dir = prompts_dir
        self._dirs = [*overrides, prompts_dir]

    def _version_numbers(self, folder: Path) -> set[int]:
        found: set[int] = set()
        for path in folder.glob("v*.md") if folder.is_dir() else []:
            match = _VERSION_FILE.match(path.name)
            if match:
                found.add(int(match.group(1)))
        return found

    def versions(self, name: str) -> list[str]:
        found: set[int] = set()
        for base in self._dirs:
            found |= self._version_numbers(base / name)
        return [f"v{n}" for n in sorted(found)]

    def source(self, name: str, version: str) -> Path:
        """The file a version is read from (the most specific folder that has it)."""
        for base in self._dirs:
            path = base / name / f"{version}.md"
            if path.is_file():
                return path
        raise ConfigError(f"Prompt '{name}' has no version {version}")

    def load(self, name: str, version: str | None = None) -> Prompt:
        available = self.versions(name)
        if not available:
            raise ConfigError(f"No prompts found for '{name}' in {self._dir / name}")
        chosen = version or available[-1]
        if chosen not in available:
            raise ConfigError(f"Prompt '{name}' has no version {chosen} (available: {available})")
        text = self.source(name, chosen).read_text()
        sha = hashlib.sha256(text.encode()).hexdigest()[:12]
        return Prompt(name=name, version=chosen, text=text, sha=sha)

    def names(self) -> list[str]:
        found = {
            p.name
            for base in self._dirs
            if base.is_dir()
            for p in base.iterdir()
            if p.is_dir() and self._version_numbers(p)
        }
        return sorted(found)
