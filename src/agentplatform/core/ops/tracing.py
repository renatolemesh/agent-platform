"""OpenTelemetry tracing (plan 10 §1). Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set; without it every span is a no-op.
Span attributes carry IDs, names, counts and costs only — never message text or personal data."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from opentelemetry import trace

log = logging.getLogger(__name__)
_initialized = False


def init_tracing(component: str) -> bool:
    """Idempotent. Returns True when an exporter was configured."""
    global _initialized
    if _initialized:
        return True
    from agentplatform.settings import get_settings
    s = get_settings()
    if not s.otel_exporter_otlp_endpoint:
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    provider = TracerProvider(resource=Resource.create({
        "service.name": s.otel_service_name, "service.namespace": "agentplatform",
        "deployment.environment": s.app_env, "agentplatform.component": component}))
    endpoint = s.otel_exporter_otlp_endpoint.rstrip("/")
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    _initialized = True
    log.info("tracing enabled (%s)", component)
    return True


_tracer = trace.get_tracer("agentplatform")


@contextmanager
def span(name: str, **attrs) -> Iterator[trace.Span]:
    with _tracer.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v if isinstance(v, (str, bool, int, float)) else str(v))
        yield sp
