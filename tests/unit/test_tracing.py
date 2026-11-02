"""Tracing is a no-op without an endpoint and, once a provider exists, spans carry IDs/route templates only."""
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentplatform.core.ops.tracing import init_tracing, span


def test_noop_without_endpoint_then_records_spans(monkeypatch):
    from agentplatform.settings import get_settings
    monkeypatch.setattr(get_settings(), "otel_exporter_otlp_endpoint", None)
    assert init_tracing("test") is False
    with span("noop", tenant_id="t1"):
        pass                                        # no provider: must not raise
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    trace.set_tracer_provider(provider)
    with span("llm.chat", node="respond", cost_usd=0.001, tenant_id=None):
        pass
    got = exp.get_finished_spans()
    assert got and got[-1].name == "llm.chat" and got[-1].attributes["node"] == "respond"
    assert "tenant_id" not in got[-1].attributes   # None attributes are dropped
