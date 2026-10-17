"""OpenTelemetry spans per stage/tool call (spec 5.3).

Emits OTEL spans with ``run_id`` as a trace attribute so CloudWatch / AgentCore
Observability can show them. The exporter is configurable and a NO-OP locally:
with no endpoint configured, spans are created against the default (no-op) tracer
provider, so nothing is exported and tests stay offline and silent.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace

_tracer = trace.get_tracer("kb_agent")


@contextmanager
def stage_span(name: str, run_id: str, **attributes: str) -> Iterator[None]:
    """Open a span for a stage/tool call, tagged with ``run_id``.

    No-op locally (no exporter configured). Attributes are recorded on the span
    for backends that collect them.
    """
    with _tracer.start_as_current_span(name) as span:
        span.set_attribute("run_id", run_id)
        for key, value in attributes.items():
            span.set_attribute(key, value)
        yield


def configure_tracing(endpoint: str) -> None:  # pragma: no cover - needs an OTLP endpoint
    """Wire an OTLP exporter when an endpoint is configured (spec 5.3).

    Called from the entrypoint only when ``KB_AGENT_OTEL_ENDPOINT`` is non-empty;
    otherwise the default no-op tracer provider is used and nothing is exported.
    """
    if not endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "kb-integration-agent"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
