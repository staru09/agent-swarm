from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from swarmguard.gateway import ExecutionCoordinator, GatewayConfig, NatsCancellationSender, PolicyGateway
from swarmguard.policy import Decision
from swarmguard.harness import HarnessRecord, encode_signed_harness_record
from swarmguard.protocol import (
    Event,
    EventKind,
    ExecutionStatus,
    ToolRequest,
    ToolResponse,
    agent_lifecycle_subject,
    event_subject,
    parse_agent_lifecycle_subject,
    tool_cancel_subject,
)


TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


class FixedPolicy:
    def __init__(self, decision: Decision):
        self.decision = decision
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def evaluate(self, agent_id: str, tool: str, arguments: dict[str, Any]) -> Decision:
        self.calls.append((agent_id, tool, dict(arguments)))
        return self.decision


class RecordingEvents:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)


class ScriptedWorker:
    def __init__(self, *outcomes: ToolResponse | BaseException):
        self.outcomes = list(outcomes)
        self.requests: list[ToolRequest] = []

    async def request(self, tool: str, request: ToolRequest, timeout: float) -> ToolResponse:
        self.requests.append(request)
        if not self.outcomes:
            raise AssertionError("worker called more times than expected")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class EvidenceWorker:
    def __init__(self, events: RecordingEvents, *outcomes: ToolResponse | BaseException):
        self.events = events
        self.outcomes = list(outcomes)
        self.requests: list[ToolRequest] = []

    async def request(self, tool: str, request: ToolRequest, timeout: float) -> ToolResponse:
        self.requests.append(request)
        await self.events.publish(
            Event(
                run_id=request.run_id,
                agent_id=request.agent_id,
                agent_instance_id=request.agent_instance_id,
                step_id=request.step_id,
                tool_call_id=request.tool_call_id,
                attempt=request.attempt,
                sequence=request.sequence,
                idempotency_key=request.idempotency_key,
                traceparent=request.traceparent,
                tracestate=request.tracestate,
                kind=EventKind.TOOL_STARTED,
                payload={"request_id": request.request_id, "tool": tool, "state": "started"},
            )
        )
        if not self.outcomes:
            return response_for(request, result={"ok": True})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class BlockingWorker:
    def __init__(self) -> None:
        self.requests: list[ToolRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def request(self, tool: str, request: ToolRequest, timeout: float) -> ToolResponse:
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return response_for(request, result={"ok": True})


class BudgetWorker:
    def __init__(self, clock: "FakeClock", *, first_attempt_elapsed: float):
        self.clock = clock
        self.first_attempt_elapsed = first_attempt_elapsed
        self.requests: list[ToolRequest] = []
        self.timeouts: list[float] = []

    async def request(self, tool: str, request: ToolRequest, timeout: float) -> ToolResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if len(self.requests) == 1:
            self.clock.advance(self.first_attempt_elapsed)
            raise asyncio.TimeoutError()
        return response_for(request, result={"ok": True})


class TimeoutWorker:
    def __init__(self) -> None:
        self.requests: list[ToolRequest] = []

    async def request(self, tool: str, request: ToolRequest, timeout: float) -> ToolResponse:
        self.requests.append(request)
        raise asyncio.TimeoutError()


class RecordingCancellationSender:
    def __init__(self) -> None:
        self.requests: list[ToolRequest] = []

    async def cancel(self, request: ToolRequest) -> None:
        self.requests.append(request)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def request_for(**overrides: Any) -> ToolRequest:
    data = {
        "request_id": "req-1",
        "run_id": "run-1",
        "agent_instance_id": "agent-instance-1",
        "step_id": "step-1",
        "tool_call_id": "tool-call-1",
        "attempt": 1,
        "sequence": 7,
        "traceparent": TRACEPARENT,
        "tracestate": "vendor=value",
        "tool": "safe_shell",
        "arguments": {"command": "uname -s"},
    }
    data.update(overrides)
    return ToolRequest(**data)


def response_for(request: ToolRequest, **overrides: Any) -> ToolResponse:
    data = {
        "request_id": request.request_id,
        "run_id": request.run_id,
        "agent_instance_id": request.agent_instance_id,
        "step_id": request.step_id,
        "tool_call_id": request.tool_call_id,
        "attempt": request.attempt,
        "sequence": request.sequence,
        "idempotency_key": request.idempotency_key,
        "traceparent": request.traceparent,
        "tracestate": request.tracestate,
        "allowed": True,
        "result": {"ok": True},
    }
    data.update(overrides)
    return ToolResponse(**data)


def subject(run_id: str = "run-1", agent_id: str = "operator") -> str:
    return f"swarm.{run_id}.agent.{agent_id}.tool.request"


def coordinator(
    worker: Any,
    events: RecordingEvents,
    *,
    policy: FixedPolicy | None = None,
    max_attempts: int = 2,
    timeout: float = 0.05,
    request_deadline: float | None = None,
    clock: Callable[[], float] | None = None,
    wall_clock: Callable[[], float] | None = None,
    cancellation_sender: RecordingCancellationSender | None = None,
) -> ExecutionCoordinator:
    return ExecutionCoordinator(
        policy=policy or FixedPolicy(Decision(True, "tool allowed")),
        worker_client=worker,
        event_publisher=events.publish,
        config=GatewayConfig(
            tool_timeout_seconds=timeout,
            max_attempts=max_attempts,
            request_deadline_seconds=request_deadline,
        ),
        monotonic=clock,
        wall_clock=wall_clock,
        cancellation_sender=cancellation_sender,
    )


def envelope(event_or_response: Event | ToolResponse) -> dict[str, Any]:
    return {
        "run_id": event_or_response.run_id,
        "agent_id": event_or_response.agent_id,
        "agent_instance_id": event_or_response.agent_instance_id,
        "step_id": event_or_response.step_id,
        "tool_call_id": event_or_response.tool_call_id,
        "attempt": event_or_response.attempt,
        "sequence": event_or_response.sequence,
        "idempotency_key": event_or_response.idempotency_key,
        "traceparent": event_or_response.traceparent,
        "tracestate": event_or_response.tracestate,
        "trace_id": event_or_response.trace_id,
        "parent_id": event_or_response.parent_id,
        "deadline_at": event_or_response.deadline_at,
    }


def canonical_events(events: list[Event]) -> list[Event]:
    return [event for event in events if event.payload.get("compatibility") != "legacy_policy_decision"]


def compatibility_events(events: list[Event]) -> list[Event]:
    return [event for event in events if event.payload.get("compatibility") == "legacy_policy_decision"]


@pytest.mark.asyncio
async def test_allowed_request_propagates_envelope_and_lifecycle_order() -> None:
    original = request_for()
    events = RecordingEvents()
    worker = ScriptedWorker(response_for(original))

    response = await coordinator(worker, events).handle_tool_request(subject(), original)

    expected_envelope = envelope(worker.requests[0])
    assert expected_envelope["agent_id"] == "operator"
    assert expected_envelope["idempotency_key"] != original.idempotency_key
    assert envelope(worker.requests[0]) == expected_envelope
    assert envelope(response) == expected_envelope
    canonical = canonical_events(events.events)
    assert [event.kind for event in canonical] == [
        EventKind.TOOL_REQUESTED,
        EventKind.TOOL_POLICY_DECIDED,
        EventKind.TOOL_DISPATCHED,
        EventKind.TOOL_COMPLETED,
    ]
    assert [event.payload.get("state") for event in canonical] == [
        "requested",
        "policy_decided",
        "dispatched",
        "completed",
    ]
    assert all(envelope(event) == expected_envelope for event in canonical)
    assert canonical[1].payload == {
        "request_id": "req-1",
        "tool": "safe_shell",
        "decision": "allowed",
        "reason": "tool allowed",
        "state": "policy_decided",
    }


@pytest.mark.asyncio
async def test_policy_decision_emits_legacy_allowed_compatibility_event() -> None:
    original = request_for()
    events = RecordingEvents()
    worker = ScriptedWorker(response_for(original))

    await coordinator(worker, events).handle_tool_request(subject(), original)

    compat = compatibility_events(events.events)
    assert len(compat) == 1
    assert compat[0].kind == EventKind.TOOL_ALLOWED
    assert envelope(compat[0]) == envelope(worker.requests[0])
    assert compat[0].payload == {
        "request_id": "req-1",
        "tool": "safe_shell",
        "decision": "allowed",
        "reason": "tool allowed",
        "state": "allowed",
        "compatibility": "legacy_policy_decision",
    }


@pytest.mark.asyncio
async def test_client_agent_id_conflict_is_rejected_before_policy() -> None:
    events = RecordingEvents()
    policy = FixedPolicy(Decision(True, "tool allowed"))
    worker = ScriptedWorker()

    response = await coordinator(worker, events, policy=policy).handle_tool_request(
        subject(agent_id="operator"),
        request_for(agent_id="intruder"),
    )

    assert response.allowed is False
    assert "agent_id does not match authenticated subject namespace" in str(response.error)
    assert policy.calls == []
    assert worker.requests == []
    assert events.events == []


@pytest.mark.asyncio
async def test_denied_request_records_requested_before_policy_and_never_dispatches() -> None:
    events = RecordingEvents()
    policy = FixedPolicy(Decision(False, "safe_shell is not allowed"))
    worker = ScriptedWorker()

    response = await coordinator(worker, events, policy=policy).handle_tool_request(subject(), request_for())

    assert response.allowed is False
    assert response.execution_status == ExecutionStatus.DENIED
    assert response.reason == "safe_shell is not allowed"
    assert worker.requests == []
    canonical = canonical_events(events.events)
    assert [event.kind for event in canonical] == [
        EventKind.TOOL_REQUESTED,
        EventKind.TOOL_POLICY_DECIDED,
        EventKind.TOOL_DENIED,
    ]
    assert [event.payload["state"] for event in canonical] == ["requested", "policy_decided", "denied"]


@pytest.mark.asyncio
async def test_policy_decision_emits_legacy_denied_compatibility_event() -> None:
    events = RecordingEvents()
    policy = FixedPolicy(Decision(False, "safe_shell is not allowed"))
    worker = ScriptedWorker()

    await coordinator(worker, events, policy=policy).handle_tool_request(subject(), request_for())

    compat = compatibility_events(events.events)
    assert len(compat) == 1
    assert compat[0].kind == EventKind.TOOL_DENIED
    assert compat[0].payload == {
        "request_id": "req-1",
        "tool": "safe_shell",
        "decision": "denied",
        "reason": "safe_shell is not allowed",
        "state": "denied",
        "compatibility": "legacy_policy_decision",
    }


@pytest.mark.asyncio
async def test_identity_mismatch_is_rejected_before_policy() -> None:
    events = RecordingEvents()
    policy = FixedPolicy(Decision(True, "tool allowed"))
    worker = ScriptedWorker()

    response = await coordinator(worker, events, policy=policy).handle_tool_request(
        subject("run-from-subject"),
        request_for(run_id="run-from-body"),
    )

    assert response.allowed is False
    assert "run_id does not match authenticated subject namespace" in str(response.error)
    assert policy.calls == []
    assert worker.requests == []
    assert events.events == []


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_reuses_in_flight_result_without_second_execution() -> None:
    request = request_for()
    events = RecordingEvents()
    worker = BlockingWorker()
    gateway = coordinator(worker, events)

    first = asyncio.create_task(gateway.handle_tool_request(subject(), request))
    await worker.started.wait()
    duplicate = asyncio.create_task(gateway.handle_tool_request(subject(), request))
    await asyncio.sleep(0)
    worker.release.set()

    first_response, duplicate_response = await asyncio.gather(first, duplicate)

    assert len(worker.requests) == 1
    assert first_response == duplicate_response
    assert duplicate_response.execution_status == ExecutionStatus.COMPLETED


@pytest.mark.asyncio
async def test_gateway_owned_idempotency_ignores_changed_client_key_for_same_logical_call() -> None:
    first = request_for(idempotency_key="client-key-1")
    duplicate = request_for(idempotency_key="client-key-2")
    events = RecordingEvents()
    worker = ScriptedWorker(response_for(first))
    gateway = coordinator(worker, events)

    first_response = await gateway.handle_tool_request(subject(), first)
    second_response = await gateway.handle_tool_request(subject(), duplicate)

    assert len(worker.requests) == 1
    assert second_response == first_response
    assert worker.requests[0].idempotency_key != "client-key-1"
    assert first_response.idempotency_key == worker.requests[0].idempotency_key


@pytest.mark.asyncio
async def test_completed_idempotency_key_reuses_cached_result_without_second_execution() -> None:
    request = request_for()
    events = RecordingEvents()
    worker = ScriptedWorker(response_for(request))
    gateway = coordinator(worker, events)

    first_response = await gateway.handle_tool_request(subject(), request)
    second_response = await gateway.handle_tool_request(subject(), request)

    assert len(worker.requests) == 1
    assert second_response == first_response


@pytest.mark.asyncio
async def test_conflicting_idempotency_key_is_rejected_without_worker_execution() -> None:
    request = request_for()
    conflict = request_for(arguments={"command": "date"}, idempotency_key=request.idempotency_key)
    events = RecordingEvents()
    worker = ScriptedWorker(response_for(request))
    gateway = coordinator(worker, events)

    await gateway.handle_tool_request(subject(), request)
    response = await gateway.handle_tool_request(subject(), conflict)

    assert len(worker.requests) == 1
    assert response.allowed is False
    assert "idempotency key conflict" in str(response.error)


@pytest.mark.asyncio
async def test_integrated_worker_evidence_keeps_single_logical_terminal_event_ordered() -> None:
    events = RecordingEvents()
    request = request_for()
    worker = EvidenceWorker(events, response_for(request))

    response = await coordinator(worker, events).handle_tool_request(subject(), request)

    assert response.execution_status == ExecutionStatus.COMPLETED
    assert [event.payload["state"] for event in canonical_events(events.events)] == [
        "requested",
        "policy_decided",
        "dispatched",
        "started",
        "completed",
    ]
    terminal_events = [
        event for event in events.events if event.kind in {EventKind.TOOL_COMPLETED, EventKind.TOOL_FAILED}
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0].payload["state"] == "completed"


@pytest.mark.asyncio
async def test_retryable_timeout_retries_with_incremented_attempt() -> None:
    request = request_for()
    events = RecordingEvents()
    worker = ScriptedWorker(asyncio.TimeoutError(), response_for(request_for(attempt=2), attempt=2))

    response = await coordinator(worker, events, max_attempts=2).handle_tool_request(subject(), request)

    assert [seen.attempt for seen in worker.requests] == [1, 2]
    assert response.execution_status == ExecutionStatus.COMPLETED
    assert response.attempt == 2
    assert [event.payload["state"] for event in events.events].count("dispatched") == 2


@pytest.mark.asyncio
async def test_retry_exhaustion_returns_timed_out_and_publishes_dead_letter() -> None:
    events = RecordingEvents()
    worker = ScriptedWorker(asyncio.TimeoutError(), asyncio.TimeoutError())

    response = await coordinator(worker, events, max_attempts=2).handle_tool_request(subject(), request_for())

    assert response.execution_status == ExecutionStatus.TIMED_OUT
    assert response.allowed is True
    assert [seen.attempt for seen in worker.requests] == [1, 2]
    assert events.events[-2].kind == EventKind.TOOL_TIMED_OUT
    assert events.events[-1].kind == EventKind.TOOL_DEAD_LETTER
    assert events.events[-1].payload["state"] == "dead_letter"


@pytest.mark.asyncio
async def test_retry_attempt_timeout_is_capped_by_remaining_request_deadline() -> None:
    clock = FakeClock()
    events = RecordingEvents()
    worker = BudgetWorker(clock, first_attempt_elapsed=0.08)

    response = await coordinator(
        worker,
        events,
        max_attempts=3,
        timeout=0.10,
        request_deadline=0.10,
        clock=clock.monotonic,
        wall_clock=lambda: 1000.0,
    ).handle_tool_request(subject(), request_for())

    assert response.execution_status == ExecutionStatus.COMPLETED
    assert [request.attempt for request in worker.requests] == [1, 2]
    assert worker.timeouts == [pytest.approx(0.10), pytest.approx(0.02)]
    assert worker.requests[0].deadline_at == pytest.approx(1000.10)
    assert worker.requests[1].deadline_at == pytest.approx(1000.10)


@pytest.mark.asyncio
async def test_retry_stops_when_request_deadline_budget_is_exhausted() -> None:
    clock = FakeClock()
    events = RecordingEvents()
    worker = BudgetWorker(clock, first_attempt_elapsed=0.11)

    response = await coordinator(
        worker,
        events,
        max_attempts=3,
        timeout=0.10,
        request_deadline=0.10,
        clock=clock.monotonic,
    ).handle_tool_request(subject(), request_for())

    assert response.execution_status == ExecutionStatus.TIMED_OUT
    assert [request.attempt for request in worker.requests] == [1]
    assert events.events[-2].kind == EventKind.TOOL_TIMED_OUT


@pytest.mark.asyncio
async def test_timeout_sends_worker_cancellation_before_single_logical_terminal() -> None:
    clock = FakeClock()
    events = RecordingEvents()
    worker = TimeoutWorker()
    cancellation = RecordingCancellationSender()

    response = await coordinator(
        worker,
        events,
        max_attempts=1,
        timeout=0.10,
        request_deadline=0.10,
        clock=clock.monotonic,
        wall_clock=lambda: 1000.0,
        cancellation_sender=cancellation,
    ).handle_tool_request(subject(), request_for())

    assert response.execution_status == ExecutionStatus.TIMED_OUT
    assert [request.tool_call_id for request in cancellation.requests] == ["tool-call-1"]
    terminal_events = [
        event for event in events.events if event.kind in {EventKind.TOOL_COMPLETED, EventKind.TOOL_FAILED, EventKind.TOOL_TIMED_OUT, EventKind.TOOL_CANCELLED}
    ]
    assert [event.kind for event in terminal_events] == [EventKind.TOOL_TIMED_OUT]


@pytest.mark.asyncio
async def test_retryable_worker_failure_exhaustion_returns_failed_and_dead_letters() -> None:
    failed = response_for(
        request_for(),
        error="boom",
        result=None,
        execution_status=ExecutionStatus.FAILED,
    )
    events = RecordingEvents()
    worker = ScriptedWorker(failed, failed)

    response = await coordinator(worker, events, max_attempts=2).handle_tool_request(subject(), request_for())

    assert response.execution_status == ExecutionStatus.FAILED
    assert [seen.attempt for seen in worker.requests] == [1, 2]
    assert events.events[-1].kind == EventKind.TOOL_DEAD_LETTER


@pytest.mark.asyncio
async def test_cancelled_in_flight_request_returns_cancelled_terminal_response() -> None:
    request = request_for()
    events = RecordingEvents()
    worker = BlockingWorker()
    gateway = coordinator(worker, events)

    running = asyncio.create_task(gateway.handle_tool_request(subject(), request))
    await worker.started.wait()
    assert gateway.cancel(request.idempotency_key or "") is True
    worker.release.set()

    response = await running

    assert response.execution_status == ExecutionStatus.CANCELLED
    assert events.events[-1].kind == EventKind.TOOL_CANCELLED
    assert events.events[-1].payload["state"] == "cancelled"


class CapturingNats:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, dict[str, str] | None]] = []

    async def publish(self, subject: str, data: bytes, headers: dict[str, str] | None = None) -> None:
        self.published.append((subject, data, headers))


class LifecycleMsg:
    def __init__(self, subject: str, data: bytes):
        self.subject = subject
        self.data = data


def test_agent_lifecycle_subject_parser_accepts_exact_shape_and_rejects_spoofed_shapes() -> None:
    assert agent_lifecycle_subject("run-1", "operator") == "swarm.run-1.agent.operator.lifecycle"
    assert parse_agent_lifecycle_subject("swarm.run-1.agent.operator.lifecycle") == ("run-1", "operator")

    with pytest.raises(ValueError):
        parse_agent_lifecycle_subject("swarm.run-1.agent.operator.audit")
    with pytest.raises(ValueError):
        parse_agent_lifecycle_subject("audit.run-1.run.started")


@pytest.mark.asyncio
async def test_gateway_republishes_valid_lifecycle_record_to_audit_with_trusted_credentials() -> None:
    nc = CapturingNats()
    gateway = PolicyGateway(FixedPolicy(Decision(True, "unused")))  # type: ignore[arg-type]
    gateway.nc = nc
    record = HarnessRecord(
        kind="run.ended",
        run_id="run-1",
        agent_id="operator",
        payload={"status": "completed"},
        sequence=7,
    )

    await gateway.handle_lifecycle(LifecycleMsg(agent_lifecycle_subject("run-1", "operator"), encode_signed_harness_record(record)))

    assert len(nc.published) == 1
    subject_used, data, headers = nc.published[0]
    event = Event.model_validate_json(data)
    assert subject_used == event_subject("run-1", EventKind.RUN_COMPLETED)
    assert headers == {"Nats-Msg-Id": event.event_id}
    assert event.agent_id == "operator"
    assert event.payload["status"] == "completed"


@pytest.mark.asyncio
async def test_gateway_rejects_lifecycle_spoof_mismatch_and_malformed_payloads() -> None:
    nc = CapturingNats()
    gateway = PolicyGateway(FixedPolicy(Decision(True, "unused")))  # type: ignore[arg-type]
    gateway.nc = nc
    mismatched = HarnessRecord(kind="run.started", run_id="run-2", agent_id="operator")
    spoofed = HarnessRecord(kind="run.started", run_id="run-1", agent_id="analyst")

    await gateway.handle_lifecycle(
        LifecycleMsg(agent_lifecycle_subject("run-1", "operator"), encode_signed_harness_record(mismatched))
    )
    await gateway.handle_lifecycle(
        LifecycleMsg(agent_lifecycle_subject("run-1", "operator"), encode_signed_harness_record(spoofed))
    )
    await gateway.handle_lifecycle(LifecycleMsg(agent_lifecycle_subject("run-1", "operator"), b"{not-json"))

    assert nc.published == []


@pytest.mark.asyncio
async def test_nats_cancellation_sender_routes_by_tool_and_carries_full_canonical_identity() -> None:
    nc = CapturingNats()
    request = request_for(agent_id="operator")

    await NatsCancellationSender(nc).cancel(request)

    assert len(nc.published) == 1
    subject_used, data, _headers = nc.published[0]
    assert subject_used == tool_cancel_subject("safe_shell", "tool-call-1")
    assert subject_used == "private.tool_call.safe_shell.tool-call-1.cancel"

    payload = ToolRequest.model_validate_json(data)
    assert payload.run_id == "run-1"
    assert payload.agent_id == "operator"
    assert payload.agent_instance_id == "agent-instance-1"
    assert payload.tool == "safe_shell"
    assert payload.tool_call_id == "tool-call-1"
    assert payload.attempt == 1
    assert payload.idempotency_key == request.idempotency_key
    assert payload.idempotency_key is not None


@pytest.mark.asyncio
async def test_cancel_publishes_attempt_scoped_identity_for_in_flight_request() -> None:
    request = request_for()
    events = RecordingEvents()
    worker = BlockingWorker()
    cancellation = RecordingCancellationSender()
    gateway = coordinator(worker, events, cancellation_sender=cancellation)

    running = asyncio.create_task(gateway.handle_tool_request(subject(), request))
    await worker.started.wait()
    assert gateway.cancel(request.idempotency_key or "") is True
    worker.release.set()
    await running
    await asyncio.sleep(0)

    assert cancellation.requests, "expected at least one cancellation to be sent"
    for sent in cancellation.requests:
        assert sent.run_id == "run-1"
        assert sent.agent_id == "operator"
        assert sent.agent_instance_id == "agent-instance-1"
        assert sent.tool == "safe_shell"
        assert sent.tool_call_id == "tool-call-1"
        assert sent.attempt == 1
        assert sent.idempotency_key == worker.requests[0].idempotency_key
        assert sent.idempotency_key is not None


def test_worker_request_subject_and_dead_letter_subject_remain_nats_compatible() -> None:
    config = GatewayConfig()

    assert config.worker_subject("safe_shell") == "private.tool.safe_shell.execute"
    assert config.dead_letter_subject("run-1") == "deadletter.run-1.tool"


@pytest.mark.asyncio
async def test_workspace_read_path_is_canonicalized_after_policy_allows(tmp_path: Path) -> None:
    relative = Path("README.md")
    request = request_for(tool="workspace_read", arguments={"path": str(relative)})
    events = RecordingEvents()
    worker = ScriptedWorker(response_for(request))

    await coordinator(worker, events).handle_tool_request(subject(), request)

    assert worker.requests[0].arguments["path"] == str(relative.resolve())


@pytest.mark.asyncio
async def test_non_dispatchable_manifest_is_denied_before_worker_dispatch(tmp_path: Path) -> None:
    manifest_path = tmp_path / "tools.yaml"
    manifest_path.write_text(
        """
tools:
  - version: 1
    name: safe_shell
    description: decoy shell probe
    input_schema:
      type: object
      properties:
        command: {type: string}
      required: [command]
    output_schema: {type: object}
    sensitivity: high
    timeout_seconds: 1
    retry_policy: {max_attempts: 1}
    worker: {subject: private.tool.safe_shell.execute, capability: shell}
    resource_limits: {max_output_bytes: 1000}
    classification: normal
    dispatchable: false
""",
        encoding="utf-8",
    )
    events = RecordingEvents()
    worker = ScriptedWorker()
    gateway = ExecutionCoordinator(
        policy=FixedPolicy(Decision(True, "tool allowed")),
        worker_client=worker,
        event_publisher=events.publish,
        config=GatewayConfig(tool_timeout_seconds=0.05, max_attempts=2),
        tool_manifest_path=manifest_path,
    )

    response = await gateway.handle_tool_request(subject(), request_for())

    assert response.allowed is False
    assert response.execution_status == ExecutionStatus.DENIED
    assert response.reason == "tool manifest is not dispatchable: safe_shell"
    assert worker.requests == []
    assert events.events[-1].kind == EventKind.TOOL_DENIED
