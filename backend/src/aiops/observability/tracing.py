"""OpenTelemetry tracing: investigation -> agent -> tool call / LLM call (PR-041).

Off by default: until ``setup_tracing`` installs an SDK provider (only when an OTLP
endpoint is configured), ``tracer()`` is the OpenTelemetry API's no-op tracer, so spans
cost next to nothing and nothing leaves the process.

Span names and attributes follow the OpenTelemetry GenAI semantic conventions where they
exist (``invoke_agent {agent}``, ``chat {model}``, ``execute_tool {tool}``,
``gen_ai.usage.input_tokens`` ...), plus ``aiops.*`` attributes (investigation id, model
role, cost, status). Prompts, tool arguments and tool output are NEVER recorded: they can
hold customer data (redaction happens later, in the toolset).

The MCP SDK already creates client spans and propagates the trace context to the MCP
servers, so once tracing is on, ``execute_tool`` spans have the MCP request as a child.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from aiops import __version__
from aiops.core.config import TracingConfig

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

log = logging.getLogger(__name__)

TRACER_NAME = "aiops"
OTLP_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
TRACES_PATH = "/v1/traces"

#: The provider ``setup_tracing`` (or a test) installed; None = the global/no-op provider.
_provider: TracerProvider | None = None

AttributeValue = str | bool | int | float
Attributes = Mapping[str, AttributeValue | None]


def tracer() -> Tracer:
    if _provider is not None:
        return _provider.get_tracer(TRACER_NAME, __version__)
    return trace.get_tracer(TRACER_NAME, __version__)


def use_provider(provider: TracerProvider | None) -> None:
    """Route this module's spans to ``provider`` (tests: an in-memory exporter) without
    touching the process-global provider, which OpenTelemetry lets you set only once."""
    global _provider
    _provider = provider


def endpoint(config: TracingConfig) -> str | None:
    """The OTLP/HTTP traces URL from the profile, else ``$OTEL_EXPORTER_OTLP_ENDPOINT``."""
    base = config.otlp_endpoint or os.environ.get(OTLP_ENV, "").strip() or None
    if base is None:
        return None
    base = base.rstrip("/")
    return base if base.endswith(TRACES_PATH) else base + TRACES_PATH


def setup_tracing(config: TracingConfig) -> bool:
    """Install an SDK provider exporting over OTLP/HTTP when an endpoint is configured.

    Idempotent; returns whether tracing is on. The exporter batches in a background
    thread and never blocks or fails an investigation (an unreachable collector only
    logs a warning)."""
    if _provider is not None:
        return True
    url = endpoint(config)
    if url is None:
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": config.service_name, "service.version": __version__}
        ),
        sampler=ParentBased(TraceIdRatioBased(config.sample_ratio)),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=url, timeout=5)))
    trace.set_tracer_provider(provider)  # the MCP SDK's client spans join the same traces
    use_provider(provider)
    log.info("OpenTelemetry tracing on: exporting spans to %s", url)
    return True


def shutdown_tracing() -> None:
    """Flush and stop the exporter (API shutdown, end of a CLI run)."""
    global _provider
    provider, _provider = _provider, None
    if provider is not None:
        provider.shutdown()


def set_attributes(span: Span, attributes: Attributes) -> None:
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


@contextmanager
def span(name: str, attributes: Attributes | None = None) -> Iterator[Span]:
    """A span as the current context. An exception marks it ERROR and is re-raised;
    ``asyncio.CancelledError`` too (a cancelled investigation is a finished trace)."""
    with tracer().start_as_current_span(
        name, record_exception=True, set_status_on_exception=True
    ) as current:
        if attributes:
            set_attributes(current, attributes)
        yield current


def mark(span: Span, status: str, error: str | None = None) -> None:
    """``aiops.status`` + the span status: ERROR for failures, else OK."""
    span.set_attribute("aiops.status", status)
    if status in ("failed", "error", "timeout", "blocked"):
        span.set_status(Status(StatusCode.ERROR, (error or status)[:200]))
    else:
        span.set_status(Status(StatusCode.OK))


def usage_attributes(usage: Any) -> dict[str, AttributeValue]:
    """GenAI token attributes + ``aiops.cost_usd`` of a ``TokenUsage``."""
    return {
        "gen_ai.usage.input_tokens": int(usage.input_tokens),
        "gen_ai.usage.output_tokens": int(usage.output_tokens),
        "aiops.llm.calls": int(usage.calls),
        "aiops.cost_usd": float(usage.cost_usd),
    }
