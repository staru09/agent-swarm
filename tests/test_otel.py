from __future__ import annotations

import os

from swarmguard.otel import (
    current_traceparent,
    instrument_harness_record,
    start_run_span,
    start_tool_attempt_span,
    start_tool_call_span,
    start_model_step_span,
    tracing_configured,
)


def test_tracing_noops_when_no_otel_exporter_is_configured(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)

    assert tracing_configured() is False
    with start_run_span("run-1", agent_id="operator") as run_span:
        with start_model_step_span("run-1", "step-1", agent_id="operator") as step_span:
            with start_tool_call_span("run-1", "tool-call-1", tool_name="safe_shell") as call_span:
                with start_tool_attempt_span("run-1", "tool-call-1", 1, tool_name="safe_shell") as attempt_span:
                    assert run_span is not None
                    assert step_span is not None
                    assert call_span is not None
                    assert attempt_span is not None


def test_trace_hierarchy_uses_existing_context_and_low_cardinality_execution_attributes() -> None:
    traceparents: list[str | None] = []

    with start_run_span("run-1", agent_id="operator") as run_span:
        run_traceparent = current_traceparent()
        with start_model_step_span("run-1", "step-1", agent_id="operator") as step_span:
            step_traceparent = current_traceparent()
            with start_tool_call_span("run-1", "tool-call-1", tool_name="safe_shell") as call_span:
                call_traceparent = current_traceparent()
                with start_tool_attempt_span("run-1", "tool-call-1", 2, tool_name="safe_shell") as attempt_span:
                    traceparents.append(current_traceparent())

    assert len({run_traceparent, step_traceparent, call_traceparent, traceparents[0]}) == 4
    assert run_traceparent and step_traceparent and call_traceparent and traceparents[0]

    assert run_span.attributes["swarmguard.run_id"] == "run-1"
    assert step_span.attributes["swarmguard.step_id"] == "step-1"
    assert call_span.attributes["swarmguard.tool_name"] == "safe_shell"
    assert call_span.attributes["swarmguard.tool_call_id"] == "tool-call-1"
    assert attempt_span.attributes["swarmguard.attempt"] == 2
    assert "swarmguard.arguments" not in call_span.attributes


def test_harness_records_gain_trace_context_without_fabricating_missing_links() -> None:
    record = instrument_harness_record(
        kind="tool.intent",
        run_id="run-1",
        agent_id="operator",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
        tool_call_id="tool-call-1",
        traceparent=None,
    )
    assert record["traceparent"] is None

    existing = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    record = instrument_harness_record(
        kind="tool.intent",
        run_id="run-1",
        agent_id="operator",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
        tool_call_id="tool-call-1",
        traceparent=existing,
    )
    assert record["traceparent"] == existing
    assert record["run_id"] == "run-1"
    assert record["agent_instance_id"] == "agent-instance-1"
