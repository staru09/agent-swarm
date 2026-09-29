import pytest
from pydantic import ValidationError

from swarmguard import protocol
from swarmguard.protocol import (
    Event,
    agent_message_subject,
    agent_tool_subject,
    parse_agent_message_subject,
    parse_agent_tool_subject,
    tool_call_identity,
    tool_cancel_subject,
    tool_cancel_wildcard_subject,
    tool_execute_subject,
    ToolRequest,
    ToolResponse,
)


def test_subjects() -> None:
    subject = agent_tool_subject("run-1", "researcher")
    assert subject == "swarm.run-1.agent.researcher.tool.request"
    assert parse_agent_tool_subject(subject) == ("run-1", "researcher")
    message = agent_message_subject("run-1", "researcher", "analyst")
    assert message.endswith(".message.analyst")
    assert parse_agent_message_subject(message) == ("run-1", "researcher", "analyst")
    assert tool_execute_subject("web_lookup") == "private.tool.web_lookup.execute"


def test_tool_cancel_subject_scopes_by_tool_and_call_id() -> None:
    assert tool_cancel_subject("safe_shell", "tool-call-1") == "private.tool_call.safe_shell.tool-call-1.cancel"
    assert tool_cancel_subject("web_lookup", "abc") == "private.tool_call.web_lookup.abc.cancel"
    assert tool_cancel_wildcard_subject("safe_shell") == "private.tool_call.safe_shell.*.cancel"


def test_tool_call_identity_distinguishes_shared_tool_call_id_across_run_agent_and_attempt() -> None:
    base = dict(tool="safe_shell", tool_call_id="dup", arguments={"command": "x"})
    a = ToolRequest(run_id="run-A", agent_id="researcher", agent_instance_id="inst-A", attempt=1, **base)
    same_logical_retry = ToolRequest(run_id="run-A", agent_id="researcher", agent_instance_id="inst-A", attempt=2, **base)
    other_run = ToolRequest(run_id="run-B", agent_id="researcher", agent_instance_id="inst-A", attempt=1, **base)
    other_agent = ToolRequest(run_id="run-A", agent_id="analyst", agent_instance_id="inst-A", attempt=1, **base)
    other_tool = ToolRequest(run_id="run-A", agent_id="researcher", agent_instance_id="inst-A", attempt=1,
                             tool="web_lookup", tool_call_id="dup", arguments={"command": "x"})

    identity = tool_call_identity(a)
    assert identity.run_id == "run-A"
    assert identity.agent_id == "researcher"
    assert identity.agent_instance_id == "inst-A"
    assert identity.tool == "safe_shell"
    assert identity.tool_call_id == "dup"
    assert identity.attempt == 1
    assert identity.idempotency_key == a.idempotency_key

    # Every scoping dimension changes the identity, so shared tool_call_id cannot collide.
    assert tool_call_identity(same_logical_retry) != identity
    assert tool_call_identity(other_run) != identity
    assert tool_call_identity(other_agent) != identity
    assert tool_call_identity(other_tool) != identity
    # Identical requests produce identical (hashable) identities usable as dict keys.
    assert tool_call_identity(a.model_copy()) == identity
    assert {identity: "value"}[tool_call_identity(a.model_copy())] == "value"


def test_tool_call_identity_falls_back_to_request_id_when_tool_call_id_absent() -> None:
    request = ToolRequest(run_id="run-1", request_id="req-9", tool="safe_shell", arguments={"command": "x"})

    assert request.tool_call_id == "req-9"
    assert tool_call_identity(request).tool_call_id == "req-9"


@pytest.mark.parametrize(
    "subject",
    ["swarm.run.agent.researcher.tool", "audit.run.tool.requested", "swarm.run.agent.tool.request"],
)
def test_invalid_tool_subjects(subject: str) -> None:
    with pytest.raises(ValueError):
        parse_agent_tool_subject(subject)


def test_tool_request_envelope_carries_canonical_correlation_fields() -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    request = ToolRequest(
        run_id="run-1",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
        tool_call_id="tool-call-1",
        attempt=2,
        sequence=7,
        traceparent=traceparent,
        tool="safe_shell",
        arguments={"command": "uname -s"},
    )

    dumped = request.model_dump()
    assert dumped["schema_version"] == "1"
    assert dumped["run_id"] == "run-1"
    assert dumped["agent_instance_id"] == "agent-instance-1"
    assert dumped["step_id"] == "step-1"
    assert dumped["tool_call_id"] == "tool-call-1"
    assert dumped["attempt"] == 2
    assert dumped["sequence"] == 7
    assert dumped["traceparent"] == traceparent
    assert dumped["event_id"]
    assert dumped["idempotency_key"]


def test_retry_attempts_keep_idempotency_key_but_not_event_id() -> None:
    first = ToolRequest(
        run_id="run-1",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
        tool_call_id="tool-call-1",
        attempt=1,
        tool="safe_shell",
        arguments={"command": "uname -s"},
    )
    retry = ToolRequest(
        run_id="run-1",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
        tool_call_id="tool-call-1",
        attempt=2,
        tool="safe_shell",
        arguments={"command": "uname -s"},
    )

    assert retry.idempotency_key == first.idempotency_key
    assert retry.event_id != first.event_id
    assert retry.attempt == 2


def test_trace_context_accepts_w3c_traceparent_and_exposes_legacy_ids() -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    request = ToolRequest(run_id="run-1", tool="safe_shell", traceparent=traceparent)

    assert request.traceparent == traceparent
    assert request.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert request.parent_id == "00f067aa0ba902b7"


def test_tool_response_separates_policy_decision_from_execution_status() -> None:
    allowed_failure = ToolResponse(request_id="req-1", allowed=True, error="boom")
    denied = ToolResponse(request_id="req-2", allowed=False, reason="policy denied")

    assert allowed_failure.policy_decision == protocol.PolicyDecision.ALLOWED
    assert allowed_failure.execution_status == protocol.ExecutionStatus.FAILED
    assert denied.policy_decision == protocol.PolicyDecision.DENIED
    assert denied.execution_status == protocol.ExecutionStatus.DENIED


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "allowed": True,
            "policy_decision": protocol.PolicyDecision.DENIED,
        },
        {
            "allowed": False,
            "policy_decision": protocol.PolicyDecision.ALLOWED,
        },
        {
            "allowed": False,
            "policy_decision": protocol.PolicyDecision.DENIED,
            "execution_status": protocol.ExecutionStatus.FAILED,
        },
        {
            "allowed": True,
            "policy_decision": protocol.PolicyDecision.ALLOWED,
            "execution_status": protocol.ExecutionStatus.DENIED,
        },
    ],
)
def test_tool_response_rejects_contradictory_policy_and_status(kwargs: dict) -> None:
    with pytest.raises(ValidationError, match="policy_decision, allowed, and execution_status disagree"):
        ToolResponse(request_id="req-1", **kwargs)


def test_tool_response_timeout_is_explicit_and_generic_errors_remain_failed() -> None:
    timed_out = ToolResponse.timed_out(request_id="req-1", reason="tool worker timed out")
    explicit_timeout = ToolResponse(
        request_id="req-2",
        allowed=True,
        policy_decision=protocol.PolicyDecision.ALLOWED,
        execution_status=protocol.ExecutionStatus.TIMED_OUT,
        error="deadline exceeded",
    )
    arbitrary_error = ToolResponse(request_id="req-3", allowed=True, error="boom")

    assert timed_out.allowed is True
    assert timed_out.policy_decision == protocol.PolicyDecision.ALLOWED
    assert timed_out.execution_status == protocol.ExecutionStatus.TIMED_OUT
    assert timed_out.error == "tool worker timed out"
    assert explicit_timeout.execution_status == protocol.ExecutionStatus.TIMED_OUT
    assert arbitrary_error.execution_status == protocol.ExecutionStatus.FAILED


def test_tool_response_retry_attempts_keep_idempotency_key_but_not_event_id() -> None:
    first = ToolResponse(
        request_id="req-1",
        run_id="run-1",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
        tool_call_id="tool-call-1",
        attempt=1,
        allowed=True,
        result={"ok": True},
    )
    retry = ToolResponse(
        request_id="req-2",
        run_id="run-1",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
        tool_call_id="tool-call-1",
        attempt=2,
        allowed=True,
        result={"ok": True},
    )

    assert retry.idempotency_key == first.idempotency_key
    assert retry.event_id != first.event_id
    assert retry.attempt == 2


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (ToolRequest, {"run_id": "run-1", "tool": "safe_shell"}),
        (ToolResponse, {"request_id": "req-1", "allowed": True}),
        (Event, {"run_id": "run-1", "kind": "tool.requested"}),
    ],
)
def test_protocol_models_reject_unsupported_schema_versions(model: type, kwargs: dict) -> None:
    with pytest.raises(ValidationError, match="unsupported schema_version: 2"):
        model(schema_version="2", **kwargs)


def test_traceparent_rejects_mismatching_explicit_valid_trace_ids() -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    with pytest.raises(ValidationError, match="traceparent does not match trace_id"):
        ToolRequest(
            run_id="run-1",
            tool="safe_shell",
            traceparent=traceparent,
            trace_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
    with pytest.raises(ValidationError, match="traceparent does not match parent_id"):
        ToolRequest(
            run_id="run-1",
            tool="safe_shell",
            traceparent=traceparent,
            parent_id="bbbbbbbbbbbbbbbb",
        )


@pytest.mark.parametrize(
    "traceparent",
    [
        "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
    ],
)
def test_traceparent_rejects_all_zero_w3c_ids(traceparent: str) -> None:
    with pytest.raises(ValidationError, match="traceparent must not contain all-zero trace_id or parent_id"):
        ToolRequest(run_id="run-1", tool="safe_shell", traceparent=traceparent)


def test_legacy_zero_like_trace_ids_remain_supported_without_traceparent() -> None:
    request = ToolRequest(
        run_id="run-1",
        tool="safe_shell",
        trace_id="00000000000000000000000000000000",
        parent_id="0000000000000000",
    )

    assert request.trace_id == "00000000000000000000000000000000"
    assert request.parent_id == "0000000000000000"
    assert request.traceparent is None


def test_legacy_non_w3c_trace_ids_remain_supported_without_traceparent() -> None:
    request = ToolRequest(
        run_id="run-1",
        tool="safe_shell",
        trace_id="legacy-trace",
        parent_id="legacy-parent",
    )

    assert request.trace_id == "legacy-trace"
    assert request.parent_id == "legacy-parent"
    assert request.traceparent is None


@pytest.mark.parametrize(
    ("current", "next_state"),
    [
        ("requested", "policy_decided"),
        ("policy_decided", "dispatched"),
        ("dispatched", "started"),
        ("started", "completed"),
        ("started", "failed"),
        ("policy_decided", "denied"),
        ("requested", "cancelled"),
        ("dispatched", "timed_out"),
    ],
)
def test_tool_call_lifecycle_allows_expected_transitions(current: str, next_state: str) -> None:
    assert protocol.validate_tool_call_transition(current, next_state) is None


@pytest.mark.parametrize(
    ("current", "next_state"),
    [
        ("requested", "started"),
        ("policy_decided", "completed"),
        ("completed", "started"),
        ("denied", "dispatched"),
    ],
)
def test_tool_call_lifecycle_rejects_invalid_or_terminal_transitions(current: str, next_state: str) -> None:
    with pytest.raises(ValueError):
        protocol.validate_tool_call_transition(current, next_state)


def test_v1_compatibility_parses_existing_request_response_and_event_payloads() -> None:
    request = ToolRequest.model_validate(
        {
            "request_id": "req-1",
            "run_id": "run-1",
            "trace_id": "legacy-trace",
            "parent_id": "legacy-parent",
            "tool": "safe_shell",
            "arguments": {"command": "uname"},
        }
    )
    response = ToolResponse.model_validate({"request_id": "req-1", "allowed": True, "result": {"ok": True}})
    event = Event.model_validate(
        {
            "schema_version": "1",
            "event_id": "event-1",
            "run_id": "run-1",
            "agent_id": "operator",
            "kind": "tool.completed",
            "trace_id": "legacy-trace",
            "parent_id": "legacy-parent",
            "payload": {"request_id": "req-1"},
        }
    )

    assert request.tool_call_id == "req-1"
    assert request.trace_id == "legacy-trace"
    assert request.parent_id == "legacy-parent"
    assert response.policy_decision == protocol.PolicyDecision.ALLOWED
    assert response.execution_status == protocol.ExecutionStatus.COMPLETED
    assert event.trace_id == "legacy-trace"
    assert event.parent_id == "legacy-parent"
