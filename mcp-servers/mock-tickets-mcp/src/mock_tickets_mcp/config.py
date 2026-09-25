"""Server settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import quote


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _flag(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ServerSettings:
    store: str = "postgres"  # postgres | memory
    pg_host: str = "localhost"
    pg_port: int = 15432
    pg_user: str = "aiops"
    pg_password: str = field(default="", repr=False)
    pg_database: str = "aiops"
    schema: str = "tickets"
    #: Server-side guardrail: only these projects exist for clients (empty = all).
    allowed_projects: tuple[str, ...] = ("OPS",)
    max_results: int = 50
    #: Like mcp-atlassian's READ_ONLY_MODE: write tools are not even registered.
    read_only: bool = False
    base_url: str = "http://localhost:8109"
    author: str = "aiops-agent"

    @property
    def dsn(self) -> str:
        password = quote(self.pg_password, safe="")
        auth = f"{self.pg_user}:{password}" if password else self.pg_user
        return f"postgresql://{auth}@{self.pg_host}:{self.pg_port}/{self.pg_database}"

    @classmethod
    def from_env(cls) -> ServerSettings:
        projects = tuple(
            p.strip().upper() for p in _env("ALLOWED_PROJECTS", "OPS").split(",") if p.strip()
        )
        return cls(
            store=_env("TICKETS_STORE", "postgres"),
            pg_host=_env("PG_HOST", "localhost"),
            pg_port=int(_env("PG_PORT", "15432")),
            pg_user=_env("PG_USER", "aiops"),
            pg_password=os.environ.get("PG_PASSWORD", ""),
            pg_database=_env("PG_DATABASE", "aiops"),
            schema=_env("TICKETS_SCHEMA", "tickets"),
            allowed_projects=() if projects == ("*",) else projects,
            max_results=int(_env("MAX_RESULTS", "50")),
            read_only=_flag("READ_ONLY_MODE", False),
            base_url=_env("BASE_URL", "http://localhost:8109").rstrip("/"),
            author=_env("DEFAULT_AUTHOR", "aiops-agent"),
        )
