from __future__ import annotations

import contextlib
import contextvars
import os
from dataclasses import dataclass, field
from typing import Any, Iterator
from uuid import uuid4


_span_stack: contextvars.ContextVar[tuple["SpanHandle", ...]] = contextvars.ContextVar("swarmguard_span_stack", default=())
_otel_initialized = False


@dataclass
class SpanHandle:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    span_id: str = field(default_factory=lambda: uuid4().hex[:16])
    parent_span_id: str | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        if value is not None:
            self.attributes[key] = value


def tracing_configured() -> bool:
    return bool(
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.getenv("OTEL_TRACES_EXPORTER")
    )


def current_traceparent() -> str | None:
    stack = _span_stack.get()
    if not stack:
        return None
    span = stack[-1]
    return f"00-{span.trace_id}-{span.span_id}-01"


@contextlib.contextmanager
def start_run_span(run_id: str, *, agent_id: str | None = None, traceparent: str | None = None) -> Iterator[SpanHandle]:
    with _span("swarmguard.run", traceparent=traceparent, **{"swarmguard.run_id": run_id, "swarmguard.agent_id": agent_id}) as span:
        yield span


@contextlib.contextmanager
def start_model_step_span(
    run_id: str,
    step_id: str,
    *,
    agent_id: str | None = None,
    traceparent: str | None = None,
) -> Iterator[SpanHandle]:
    attrs = {"swarmguard.run_id": run_id, "swarmguard.step_id": step_id, "swarmguard.agent_id": agent_id}
    with _span("swarmguard.model_step", traceparent=traceparent, **attrs) as span:
        yield span


@contextlib.contextmanager
def start_tool_call_span(
    run_id: str,
    tool_call_id: str,
    *,
    tool_name: str | None = None,
    traceparent: str | None = None,
) -> Iterator[SpanHandle]:
    attrs = {
        "swarmguard.run_id": run_id,
        "swarmguard.tool_call_id": tool_call_id,
        "swarmguard.tool_name": tool_name,
    }
    with _span("swarmguard.tool_call", traceparent=traceparent, **attrs) as span:
        yield span


@contextlib.contextmanager
def start_tool_attempt_span(
    run_id: str,
    tool_call_id: str,
    attempt: int,
    *,
    tool_name: str | None = None,
    traceparent: str | None = None,
) -> Iterator[SpanHandle]:
    attrs = {
        "swarmguard.run_id": run_id,
        "swarmguard.tool_call_id": tool_call_id,
        "swarmguard.tool_name": tool_name,
        "swarmguard.attempt": attempt,
    }
    with _span("swarmguard.tool_attempt", traceparent=traceparent, **attrs) as span:
        yield span


def instrument_harness_record(
    *,
    kind: str,
    run_id: str,
    agent_id: str | None,
    agent_instance_id: str | None,
    step_id: str | None,
    tool_call_id: str | None,
    traceparent: str | None,
) -> dict[str, str | None]:
    return {
        "kind": kind,
        "run_id": run_id,
        "agent_id": agent_id,
        "agent_instance_id": agent_instance_id,
        "step_id": step_id,
        "tool_call_id": tool_call_id,
        "traceparent": traceparent,
    }


@contextlib.contextmanager
def _span(name: str, *, traceparent: str | None = None, **attrs: Any) -> Iterator[SpanHandle]:
    parent = _span_stack.get()[-1] if _span_stack.get() else None
    trace_id, parent_span_id = _context_from_traceparent(traceparent)
    if trace_id is None and parent is not None:
        trace_id = parent.trace_id
        parent_span_id = parent.span_id
    span = SpanHandle(name=name, trace_id=trace_id or uuid4().hex, parent_span_id=parent_span_id)
    for key, value in attrs.items():
        span.set_attribute(key, value)
    token = _span_stack.set((*_span_stack.get(), span))
    try:
        with _real_otel_span(name, span.attributes):
            yield span
    finally:
        _span_stack.reset(token)


def _context_from_traceparent(traceparent: str | None) -> tuple[str | None, str | None]:
    if not traceparent:
        return None, None
    parts = traceparent.split("-")
    if len(parts) != 4 or parts[0] != "00":
        return None, None
    trace_id, parent_id = parts[1], parts[2]
    if len(trace_id) != 32 or len(parent_id) != 16:
        return None, None
    return trace_id, parent_id


@contextlib.contextmanager
def _real_otel_span(name: str, attributes: dict[str, Any]) -> Iterator[None]:
    if not tracing_configured():
        yield
        return
    try:
        _ensure_otel_initialized()
        from opentelemetry import trace
    except Exception:
        yield
        return
    with trace.get_tracer("swarmguard").start_as_current_span(name, attributes=attributes):
        yield


def _ensure_otel_initialized() -> None:
    global _otel_initialized
    if _otel_initialized:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "swarmguard")}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _otel_initialized = True
