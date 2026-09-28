"""``logs/elasticsearch``: ES|QL via elasticsearch-mcp's ``execute_esql``, Kibana KQL links.

Works with self-managed Elasticsearch and Elastic Cloud. Field names come from
``settings.fields`` (defaults: the synthetic/ECS-like conventions below); when all
services share one index, ``settings.service_filter: true`` narrows every query to
``fields.service == <catalog logs.service_value>``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar
from urllib.parse import quote

from aiops.providers.base import ToolRequest
from aiops.providers.logs import LogScope, LogsProvider, LogWindow, iso
from aiops.providers.registry import PROVIDER_REGISTRY

EXECUTE_ESQL = "execute_esql"
SEARCH_LOGS = "search_logs"


def esql_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def esql_like(prefix: str) -> str:
    """LIKE pattern matching ``prefix`` literally, followed by anything."""
    pattern = prefix.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?") + "*"
    return esql_string(pattern)


class ElasticsearchLogs(LogsProvider):
    name: ClassVar[str] = "elasticsearch"
    mcp: ClassVar[str] = "mcp-servers/elasticsearch-mcp"
    agent_tools: ClassVar[tuple[str, ...]] = (EXECUTE_ESQL,)
    note: ClassVar[str] = "ES|QL; field names from settings.fields"
    prompt_fragment: ClassVar[str | None] = "providers/logs/elasticsearch"
    default_fields: ClassVar[dict[str, str]] = {
        "timestamp": "@timestamp",
        "level": "level",
        "message": "message",
        "message_keyword": "message.keyword",
        "service": "service",
        "trace_id": "trace_id",
    }

    def fields(self) -> dict[str, str]:
        fields = super().fields()
        fields.setdefault("message_keyword", f"{fields['message']}.keyword")
        return fields

    # -- ES|QL building blocks ------------------------------------------------------------

    def service_where(self, scope: LogScope) -> str | None:
        """ES|QL condition selecting this service's lines in a shared index."""
        if scope.service_value is None:
            return None
        return f"{scope.fields['service']} == {esql_string(scope.service_value)}"

    def source(self, scope: LogScope) -> str:
        """``FROM <index>``, narrowed to the service when the index is shared."""
        where = self.service_where(scope)
        return f"FROM {scope.index}" + (f" | WHERE {where}" if where else "")

    def service_kql(self, scope: LogScope) -> str:
        """KQL clause for UI links ('' when the index isn't shared)."""
        if scope.service_value is None:
            return ""
        return f'{scope.fields["service"]}:"{scope.service_value}"'

    def _window_eval(self, scope: LogScope, window: LogWindow) -> str:
        ts = scope.fields["timestamp"]
        return (
            f'EVAL window = CASE({ts} >= TO_DATETIME("{iso(window.start)}"), "current", "baseline")'
        )

    def _esql(
        self, query: str, window: LogWindow, columns: dict[str, str] | None = None
    ) -> ToolRequest:
        return ToolRequest(
            EXECUTE_ESQL,
            {"query": query, "start": iso(window.scope_start), "end": iso(window.end)},
            columns=columns or {},
        )

    # -- the Log agent's questions ----------------------------------------------------------

    def volume_by_level(self, scope: LogScope, window: LogWindow) -> ToolRequest:
        f = scope.fields
        return self._esql(
            f"{self.source(scope)} | {self._window_eval(scope, window)} "
            f"| STATS count = COUNT(*) BY window, level = {f['level']}",
            window,
        )

    def message_patterns(self, scope: LogScope, window: LogWindow, limit: int) -> ToolRequest:
        f = scope.fields
        levels = ", ".join(esql_string(level) for level in scope.pattern_levels)
        return self._esql(
            f"{self.source(scope)} | WHERE {f['level']} IN ({levels}) | {self._window_eval(scope, window)} "
            f"| STATS count = COUNT(*), first_seen = MIN({f['timestamp']}), last_seen = MAX({f['timestamp']}) "
            f"BY window, level = {f['level']}, msg = {f['message_keyword']} "
            f"| SORT count DESC | LIMIT {limit}",
            window,
        )

    def versions_and_startups(self, scope: LogScope, window: LogWindow) -> ToolRequest:
        f = scope.fields
        return self._esql(
            f"{self.source(scope)} | {self._window_eval(scope, window)} "
            f"| EVAL is_start = CASE({f['message']} LIKE {esql_string(scope.startup_pattern)}, 1, 0) "
            f"| STATS count = COUNT(*), starts = SUM(is_start), first_seen = MIN({f['timestamp']}) "
            f"BY window, version = {f['version']}",
            window,
        )

    def first_occurrences(
        self, scope: LogScope, window: LogWindow, phrase: str, limit: int
    ) -> ToolRequest:
        f = scope.fields
        roles = [k for k in ("timestamp", "trace_id", "version") if k in f]
        keep = ", ".join(dict.fromkeys(f[k] for k in roles))
        return self._esql(
            f'{self.source(scope)} | WHERE {f["timestamp"]} >= TO_DATETIME("{iso(window.start)}") '
            f"AND {f['message']} LIKE {esql_like(phrase)} "
            f"| KEEP {keep} | SORT {f['timestamp']} ASC | LIMIT {limit}",
            window,
            columns={f[k]: k for k in roles},
        )

    # -- links and prompt -----------------------------------------------------------------

    def ui_link(
        self,
        scope: LogScope,
        window: LogWindow,
        *,
        levels: Sequence[str] | None = None,
        phrase: str | None = None,
    ) -> str | None:
        """Kibana Discover link: ``settings.ui_link_template`` with ``{start} {end} {kql}``."""
        if not scope.link_template:
            return None
        f = scope.fields
        kql = ""
        if levels is not None:
            kql = f"{f['level']}:({' or '.join(levels)})"
        elif phrase:
            kql = f'{f["message"]}:"{phrase}"'
        service = self.service_kql(scope)
        if service:
            kql = f"{service} and ({kql})" if kql else service
        return scope.link_template.format(
            start=iso(window.start), end=iso(window.end), kql=quote(kql, safe="")
        )

    def scope_note(self, scope: LogScope) -> str:
        if not self.service_where(scope):
            return ""
        return (
            "\n\nThe index is shared by all services: every query MUST filter on this "
            f"service, e.g. `{self.source(scope)} | ...` ({SEARCH_LOGS}: add "
            f"`{self.service_kql(scope)}` to the query)."
        )


PROVIDER_REGISTRY.register(ElasticsearchLogs)
