"""``logs/loki``: LogQL via loki-mcp (``query``, ``query_range``), Grafana Explore links.

Works with self-hosted Loki and Grafana Cloud Logs. The catalog's ``logs.index_pattern``
is the **stream selector** (e.g. ``{namespace="prod"}``). Each field role in
``settings.fields`` is either a Loki **stream label** (listed in ``settings.stream_labels``,
e.g. ``app``, ``level``: matched in the selector) or a key of the JSON log line (extracted
at query time with ``| json``). ``settings.service_filter: true`` narrows every query to
the catalog's ``logs.service_value`` on ``fields.service``.

Every agent question is ONE LogQL call returning the neutral columns of
``aiops.providers.logs``:

* the incident/baseline split is a query-time label: ``label_format window=`{{ if ge
  (unixEpochMillis __timestamp__) "<incident start ms>" }}current{{ else }}baseline{{ end }}```;
* aggregates are instant metric queries over ``[baseline start, end]``
  (``count_over_time``, ``min_over_time``/``max_over_time`` of the line timestamp for
  first/last seen), several statistics joined with ``or`` and told apart by a ``stat``
  label (``label_replace``); ``table()`` pivots them back into rows;
* first occurrences are a forward log query (``query_range``) whose lines are parsed.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import quote

from aiops.providers.base import ToolRequest
from aiops.providers.logs import LogScope, LogsProvider, LogTable, LogWindow, iso
from aiops.providers.registry import PROVIDER_REGISTRY

QUERY = "query"
QUERY_RANGE = "query_range"

#: Query-time label names the queries extract into (the neutral column names).
ALIASES = {"level": "level", "message": "msg", "version": "version", "trace_id": "trace_id"}
SERVICE_ALIAS = "svc"
TS_ALIAS = "ts_ms"
#: ``ToolRequest.columns`` marker: which result shape ``table()`` should expect.
SHAPE = "__shape__"


def logql_string(value: str) -> str:
    """A LogQL double-quoted string literal (Go escaping == JSON escaping here)."""
    return json.dumps(value, ensure_ascii=False)


_RE2_SPECIAL = frozenset("\\.+*?()|[]{}^$")


def re2_escape(text: str) -> str:
    """Escape RE2 metacharacters only (``re.escape`` also escapes spaces etc.)."""
    return "".join(f"\\{ch}" if ch in _RE2_SPECIAL else ch for ch in text)


def glob_regex(glob: str) -> str:
    """``Starting*`` -> ``^Starting.*$`` (RE2). LogQL label-filter regexes match the whole
    value; the explicit anchors keep that true whatever Loki's regex simplifications do."""
    body = "".join(".*" if ch == "*" else "." if ch == "?" else re2_escape(ch) for ch in glob)
    return f"^{body}$"


def _epoch_ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def _ms_iso(value: Any) -> str | None:
    """Epoch milliseconds (a sample value) -> ``2026-09-26T10:12:13.719Z``."""
    try:
        ms = int(float(value))
    except (TypeError, ValueError):
        return None
    seconds, rest = divmod(ms, 1000)
    base = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{rest:03d}Z"


class LokiLogs(LogsProvider):
    name: ClassVar[str] = "loki"
    mcp: ClassVar[str] = "mcp-servers/loki-mcp"
    required: ClassVar[tuple[str, ...]] = ("stream_labels",)
    agent_tools: ClassVar[tuple[str, ...]] = (QUERY, QUERY_RANGE)
    note: ClassVar[str] = "LogQL; stream labels + JSON line fields from settings"
    prompt_fragment: ClassVar[str | None] = "providers/logs/loki"
    default_fields: ClassVar[dict[str, str]] = {
        "timestamp": "__timestamp__",
        "level": "level",
        "message": "message",
        "service": "app",
        "trace_id": "trace_id",
    }

    # -- LogQL building blocks ------------------------------------------------------------

    def stream_labels(self) -> set[str]:
        return {str(label) for label in self.settings.get("stream_labels", [])}

    def is_label(self, scope: LogScope, role: str) -> bool:
        """Is the role's field an indexed stream label (vs a JSON key of the line)?"""
        return scope.fields.get(role, "") in self.stream_labels()

    def selector(self, scope: LogScope, *extra: str) -> str:
        """The catalog selector, plus the service and any extra stream matchers."""
        base = scope.index.strip()
        inner = base[1:-1].strip() if base.startswith("{") and base.endswith("}") else base
        matchers = [m for m in (inner,) if m]
        if scope.service_value is not None and self.is_label(scope, "service"):
            matchers.append(f"{scope.fields['service']}={logql_string(scope.service_value)}")
        matchers.extend(extra)
        return "{" + ", ".join(matchers) + "}"

    @staticmethod
    def level_matcher(levels: Sequence[str]) -> str:
        alternation = "|".join(re2_escape(level) for level in levels)
        return f"=~{logql_string(f'^(?:{alternation})$')}"

    def pipeline(
        self,
        scope: LogScope,
        roles: Sequence[str],
        *,
        levels: Sequence[str] | None = None,
        window: LogWindow | None = None,
        line_filter: str | None = None,
    ) -> str:
        """``{selector} [|= "..."] | json alias="field", ... | drop errors | filters | window``.

        Stream-label roles are grouped on directly; JSON roles are extracted under their
        neutral alias (``msg``, ``version``, ...). Non-JSON lines are kept (their parse error
        is dropped), like documents without the field in Elasticsearch."""
        f = scope.fields
        extra: list[str] = []
        if levels is not None and self.is_label(scope, "level"):
            extra.append(f"{f['level']}{self.level_matcher(levels)}")
        query = self.selector(scope, *extra)
        if line_filter:
            query += f" |= {logql_string(line_filter)}"
        extract: dict[str, str] = {}
        for role in roles:
            if role in f and not self.is_label(scope, role):
                extract[ALIASES.get(role, role)] = f[role]
        service_json = scope.service_value is not None and not self.is_label(scope, "service")
        if service_json:
            extract[SERVICE_ALIAS] = f["service"]
        if extract:
            params = ", ".join(f"{alias}={logql_string(field)}" for alias, field in extract.items())
            query += f" | json {params} | drop __error__, __error_details__"
        if service_json and scope.service_value is not None:
            query += f" | {SERVICE_ALIAS}={logql_string(scope.service_value)}"
        if levels is not None and not self.is_label(scope, "level"):
            query += f" | level{self.level_matcher(levels)}"
        if window is not None:
            query += (
                " | label_format window=`{{ if ge (unixEpochMillis __timestamp__) "
                f'"{_epoch_ms(window.start)}" }}}}current{{{{ else }}}}baseline{{{{ end }}}}`'
            )
        return query

    def group(self, scope: LogScope, role: str) -> str:
        """The label to group a role by (the stream label, or the extracted alias)."""
        if self.is_label(scope, role):
            return scope.fields[role]
        return ALIASES.get(role, role)

    @staticmethod
    def span(window: LogWindow) -> str:
        """The instant query's range: everything from the baseline start to the end."""
        seconds = math.ceil((window.end - window.scope_start).total_seconds())
        return f"{max(seconds, 1)}s"

    @staticmethod
    def stat(expr: str, name: str) -> str:
        return f'label_replace({expr}, "stat", "{name}", "", "")'

    def _instant(self, query: str, window: LogWindow, columns: dict[str, str]) -> ToolRequest:
        return ToolRequest(
            QUERY, {"query": query, "time": iso(window.end)}, columns={SHAPE: "stats", **columns}
        )

    # -- the Log agent's questions ----------------------------------------------------------

    def volume_by_level(self, scope: LogScope, window: LogWindow) -> ToolRequest:
        level = self.group(scope, "level")
        pipe = self.pipeline(scope, ["level"], window=window)
        query = self.stat(
            f"sum by (window, {level}) (count_over_time({pipe} [{self.span(window)}]))", "count"
        )
        return self._instant(query, window, {level: "level"})

    def message_patterns(self, scope: LogScope, window: LogWindow, limit: int) -> ToolRequest:
        level = self.group(scope, "level")
        by = f"window, {level}, msg"
        pipe = self.pipeline(
            scope, ["level", "message"], levels=scope.pattern_levels, window=window
        )
        rng = self.span(window)
        ts = f"{pipe} | label_format {TS_ALIAS}=`{{{{ unixEpochMillis __timestamp__ }}}}` | unwrap {TS_ALIAS}"
        query = " or ".join(
            [
                self.stat(
                    f"topk({limit}, sum by ({by}) (count_over_time({pipe} [{rng}])))", "count"
                ),
                self.stat(f"min by ({by}) (min_over_time({ts} [{rng}]))", "first_seen"),
                self.stat(f"max by ({by}) (max_over_time({ts} [{rng}]))", "last_seen"),
            ]
        )
        return self._instant(query, window, {level: "level"})

    def versions_and_startups(self, scope: LogScope, window: LogWindow) -> ToolRequest:
        version = self.group(scope, "version")
        by = f"window, {version}"
        pipe = self.pipeline(scope, ["message", "version"], window=window)
        rng = self.span(window)
        msg = self.group(scope, "message")
        starts = f"{pipe} | {msg}=~{logql_string(glob_regex(scope.startup_pattern))}"
        ts = f"{pipe} | label_format {TS_ALIAS}=`{{{{ unixEpochMillis __timestamp__ }}}}` | unwrap {TS_ALIAS}"
        query = " or ".join(
            [
                self.stat(f"sum by ({by}) (count_over_time({pipe} [{rng}]))", "count"),
                self.stat(f"sum by ({by}) (count_over_time({starts} [{rng}]))", "starts"),
                self.stat(f"min by ({by}) (min_over_time({ts} [{rng}]))", "first_seen"),
            ]
        )
        return self._instant(query, window, {version: "version"})

    def first_occurrences(
        self, scope: LogScope, window: LogWindow, phrase: str, limit: int
    ) -> ToolRequest:
        roles = [r for r in ("message", "trace_id", "version") if r in scope.fields]
        # A cheap line filter first, when the phrase appears verbatim in the JSON line.
        verbatim = json.dumps(phrase, ensure_ascii=False)[1:-1] == phrase
        pipe = self.pipeline(scope, roles, line_filter=phrase if verbatim else None)
        msg = self.group(scope, "message")
        query = f"{pipe} | {msg}=~{logql_string('^' + re2_escape(phrase) + '.*')}"
        columns = {SHAPE: "lines"}
        for role in ("trace_id", "version"):
            if role in scope.fields:
                columns[self.group(scope, role)] = role
        return ToolRequest(
            QUERY_RANGE,
            {
                "query": query,
                "start": iso(window.start),
                "end": iso(window.end),
                "limit": limit,
                "direction": "forward",
            },
            columns=columns,
        )

    # -- results --------------------------------------------------------------------------

    def table(self, request: ToolRequest, data: Any) -> LogTable | None:
        """Pivot loki-mcp results into neutral rows.

        ``stats`` (instant vectors with a ``stat`` label): one row per label set
        (``window``, ``level``/``msg``/``version``), one column per stat; ``first_seen`` /
        ``last_seen`` epoch ms become ISO timestamps. ``lines``: one row per log line
        (``timestamp`` + the mapped labels)."""
        if not isinstance(data, dict):
            return None
        shape = request.columns.get(SHAPE)
        rename = {k: v for k, v in request.columns.items() if k != SHAPE}
        if shape == "lines":
            return self._lines_table(data, rename)
        if shape != "stats":
            return super().table(request, data)
        keys: list[str] = []
        stats: list[str] = []
        rows: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
        for series in data.get("series") or []:
            labels = {rename.get(k, k): v for k, v in (series.get("labels") or {}).items()}
            stat = str(labels.pop("stat", "count"))
            for key in labels:
                if key not in keys:
                    keys.append(key)
            if stat not in stats:
                stats.append(stat)
            ident = tuple(sorted(labels.items()))
            value = series.get("value")
            rows.setdefault(ident, {})[stat] = (
                _ms_iso(value) if stat in ("first_seen", "last_seen") else value
            )
        columns = [*stats, *keys]
        out: list[list[Any]] = []
        for ident, values in rows.items():
            if "count" in stats and values.get("count") is None:
                continue  # beyond topk: no count, no row
            labels = dict(ident)
            out.append(
                [values.get(s) for s in stats]
                + [labels.get(k) if labels.get(k) != "" else None for k in keys]
            )
        out.sort(key=lambda r: -(r[0] or 0) if stats and stats[0] == "count" else 0)
        return LogTable(columns, out)

    @staticmethod
    def _lines_table(data: dict[str, Any], rename: dict[str, str]) -> LogTable:
        roles = list(dict.fromkeys(rename.values()))
        columns = ["timestamp", *roles]
        rows: list[list[Any]] = []
        for line in data.get("lines") or []:
            labels = line.get("labels") or {}
            by_role = {rename[k]: v for k, v in labels.items() if k in rename}
            rows.append([line.get("timestamp"), *(by_role.get(r) for r in roles)])
        return LogTable(columns, rows)

    # -- links and prompt -----------------------------------------------------------------

    def ui_link(
        self,
        scope: LogScope,
        window: LogWindow,
        *,
        levels: Sequence[str] | None = None,
        phrase: str | None = None,
    ) -> str | None:
        """Grafana Explore link: ``settings.ui_link_template`` with ``{panes}`` (the Explore
        state), ``{query}`` (LogQL), ``{from_ms} {to_ms} {start} {end}``."""
        if not scope.link_template:
            return None
        if levels is not None:
            query = self.pipeline(scope, [], levels=levels)
        elif phrase:
            query = self.pipeline(scope, [], line_filter=phrase)
        else:
            query = self.pipeline(scope, [])
        uid = str(self.settings.get("grafana_datasource_uid", "loki"))
        from_ms, to_ms = _epoch_ms(window.start), _epoch_ms(window.end)
        panes = {
            "a": {
                "datasource": uid,
                "queries": [
                    {"refId": "A", "expr": query, "datasource": {"type": "loki", "uid": uid}}
                ],
                "range": {"from": str(from_ms), "to": str(to_ms)},
            }
        }
        return scope.link_template.format(
            panes=quote(json.dumps(panes, separators=(",", ":")), safe=""),
            query=quote(query, safe=""),
            from_ms=from_ms,
            to_ms=to_ms,
            start=iso(window.start),
            end=iso(window.end),
        )

    def scope_note(self, scope: LogScope) -> str:
        labels = sorted(self.stream_labels())
        note = (
            f"\n\nLoki: `{scope.index}` is the stream selector. Stream labels (match them in "
            f"`{{...}}`): {', '.join(f'`{label}`' for label in labels) or 'none'}; every other "
            "field is a key of the JSON line (`| json`)."
        )
        if scope.service_value is not None:
            note += (
                " The streams are shared by all services: every query MUST select this "
                f"service, e.g. `{self.pipeline(scope, [])}`."
            )
        return note


PROVIDER_REGISTRY.register(LokiLogs)
