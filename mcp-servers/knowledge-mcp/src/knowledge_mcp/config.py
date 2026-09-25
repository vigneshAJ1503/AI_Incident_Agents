"""Server settings from environment variables.

The database connection uses ``KNOWLEDGE_DATABASE_URL`` if set; otherwise libpq's
standard ``PGHOST`` / ``PGPORT`` / ``PGUSER`` / ``PGPASSWORD`` / ``PGDATABASE``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class ServerSettings:
    conninfo: str = field(default="", repr=False)  # "" = libpq PG* environment variables
    schema: str = "knowledge"
    ts_config: str = "english"  # must match the ingester (aiops.knowledge.ingest.TS_CONFIG)
    max_k: int = 10
    max_query_chars: int = 500
    max_filter_items: int = 10
    max_doc_chars: int = 60_000
    max_list_docs: int = 500
    statement_timeout_ms: int = 5_000
    connect_timeout_s: int = 5

    def __post_init__(self) -> None:
        for name in ("schema", "ts_config"):
            if not _IDENTIFIER.match(getattr(self, name)):
                raise ValueError(f"invalid {name}: {getattr(self, name)!r}")

    @classmethod
    def from_env(cls) -> ServerSettings:
        return cls(
            conninfo=os.environ.get("KNOWLEDGE_DATABASE_URL", "").strip(),
            schema=_env("KNOWLEDGE_SCHEMA", "knowledge"),
            ts_config=_env("KNOWLEDGE_TS_CONFIG", "english"),
            max_k=int(_env("MAX_K", "10")),
            max_query_chars=int(_env("MAX_QUERY_CHARS", "500")),
            max_doc_chars=int(_env("MAX_DOC_CHARS", "60000")),
            max_list_docs=int(_env("MAX_LIST_DOCS", "500")),
            statement_timeout_ms=int(_env("STATEMENT_TIMEOUT_MS", "5000")),
        )
