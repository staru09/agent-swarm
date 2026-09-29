from __future__ import annotations

from swarmguard.harness import HarnessRecord, InMemoryHarnessRecorder


def test_recorder_captures_framework_neutral_lifecycle_records() -> None:
    recorder = InMemoryHarnessRecorder()

    recorder.run_started(run_id="run-1", agent_id="researcher")
    recorder.agent_session_started(
        run_id="run-1",
        agent_id="researcher",
        agent_instance_id="agent-instance-1",
    )
    recorder.model_step_started(
        run_id="run-1",
        agent_id="researcher",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
    )
    recorder.tool_intent(
        run_id="run-1",
        agent_id="researcher",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
        tool_call_id="toolu-1",
        tool_name="web_lookup",
        arguments={"url": "https://example.com"},
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )
    recorder.handoff(
        run_id="run-1",
        agent_id="researcher",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
        recipient="analyst",
        message="please summarize",
    )
    recorder.model_step_ended(
        run_id="run-1",
        agent_id="researcher",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
        status="completed",
    )
    recorder.agent_session_ended(
        run_id="run-1",
        agent_id="researcher",
        agent_instance_id="agent-instance-1",
        status="completed",
    )
    recorder.run_ended(run_id="run-1", agent_id="researcher", status="completed")

    assert [record.kind for record in recorder.records] == [
        "run.started",
        "agent_session.started",
        "model_step.started",
        "tool.intent",
        "handoff",
        "model_step.ended",
        "agent_session.ended",
        "run.ended",
    ]
    assert [record.sequence for record in recorder.records] == list(range(1, 9))
    tool_intent = recorder.records[3]
    assert tool_intent.run_id == "run-1"
    assert tool_intent.agent_id == "researcher"
    assert tool_intent.agent_instance_id == "agent-instance-1"
    assert tool_intent.step_id == "step-1"
    assert tool_intent.tool_call_id == "toolu-1"
    assert tool_intent.payload == {
        "tool_name": "web_lookup",
        "arguments": {"url": "https://example.com"},
    }
    assert tool_intent.traceparent == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_recorder_deduplicates_records_when_adapter_replays_after_reconnect() -> None:
    recorder = InMemoryHarnessRecorder()
    record = HarnessRecord(
        record_id="event-1",
        sequence=99,
        kind="model_step.started",
        run_id="run-1",
        agent_id="operator",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
    )

    first = recorder.record(record)
    second = recorder.record(record)

    assert first is second
    assert len(recorder.records) == 1
    assert recorder.records[0].sequence == 1


def test_recorder_deduplicates_replayed_logical_events_with_new_record_ids() -> None:
    recorder = InMemoryHarnessRecorder()
    first = HarnessRecord(
        kind="model_step.ended",
        run_id="run-1",
        agent_id="operator",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
        payload={"status": "failed", "error": "model unavailable"},
    )
    replayed = HarnessRecord(
        kind="model_step.ended",
        run_id="run-1",
        agent_id="operator",
        agent_instance_id="agent-instance-1",
        step_id="step-1",
        payload={"status": "failed", "error": "model unavailable"},
    )

    stored = recorder.record(first)
    deduped = recorder.record(replayed)

    assert deduped is stored
    assert len(recorder.records) == 1


def test_recorder_preserves_distinct_repeated_events() -> None:
    recorder = InMemoryHarnessRecorder()

    recorder.record(
        HarnessRecord(
            kind="tool.intent",
            run_id="run-1",
            agent_id="operator",
            agent_instance_id="agent-instance-1",
            step_id="step-1",
            tool_call_id="toolu-1",
            payload={"tool_name": "safe_shell", "arguments": {"command": "uname"}},
        )
    )
    recorder.record(
        HarnessRecord(
            kind="tool.intent",
            run_id="run-1",
            agent_id="operator",
            agent_instance_id="agent-instance-1",
            step_id="step-1",
            tool_call_id="toolu-2",
            payload={"tool_name": "safe_shell", "arguments": {"command": "date"}},
        )
    )

    assert len(recorder.records) == 2
    assert [record.tool_call_id for record in recorder.records] == ["toolu-1", "toolu-2"]


def test_recorder_can_resume_sequence_after_reconnect() -> None:
    first_session = InMemoryHarnessRecorder()
    first_session.run_started(run_id="run-1", agent_id="analyst")

    resumed = InMemoryHarnessRecorder(records=first_session.records)
    resumed.agent_session_started(
        run_id="run-1",
        agent_id="analyst",
        agent_instance_id="agent-instance-2",
    )

    assert [record.sequence for record in resumed.records] == [1, 2]
    assert [record.kind for record in resumed.records] == ["run.started", "agent_session.started"]


def test_recorder_represents_cancellation_as_terminal_lifecycle_status() -> None:
    recorder = InMemoryHarnessRecorder()

    recorder.agent_session_started(
        run_id="run-1",
        agent_id="operator",
        agent_instance_id="agent-instance-1",
    )
    recorder.agent_session_ended(
        run_id="run-1",
        agent_id="operator",
        agent_instance_id="agent-instance-1",
        status="cancelled",
    )
    recorder.run_ended(run_id="run-1", agent_id="operator", status="cancelled")

    assert recorder.records[-2].payload == {"status": "cancelled"}
    assert recorder.records[-1].payload == {"status": "cancelled"}
