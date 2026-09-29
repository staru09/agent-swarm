import asyncio
import contextlib
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from swarmguard.protocol import Event, EventKind, ExecutionStatus, ToolRequest, ToolResponse
from swarmguard.tools import ToolWorker, execute_async, fetch_url


class RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:9/private")
        self.end_headers()

    def log_message(self, *_args) -> None:
        pass


def test_web_tool_rejects_plaintext_and_loopback_targets() -> None:
    # The SSRF policy now rejects non-HTTPS and loopback destinations outright,
    # so a plaintext loopback redirect server can never be reached. Detailed
    # SSRF/redirect coverage lives in tests/test_tool_sandbox.py.
    from swarmguard.tools import SsrfError

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with pytest.raises(SsrfError):
            fetch_url(f"http://127.0.0.1:{server.server_port}/redirect")
        with pytest.raises(SsrfError):
            fetch_url(f"https://127.0.0.1:{server.server_port}/redirect")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class FakeMessage:
    def __init__(self, request: ToolRequest):
        self.data = request.model_dump_json().encode()
        self.responses: list[ToolResponse] = []

    async def respond(self, data: bytes) -> None:
        self.responses.append(ToolResponse.model_validate_json(data))


class FakeNats:
    def __init__(self) -> None:
        self.published: list[Event] = []

    async def publish(self, subject: str, data: bytes, headers: dict | None = None) -> None:
        self.published.append(Event.model_validate_json(data))


async def eventually(condition, *, timeout: float = 0.2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.005)
    assert condition()


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def tool_request(**overrides) -> ToolRequest:
    data = {
        "request_id": "req-1",
        "run_id": "run-1",
        "agent_id": "operator",
        "agent_instance_id": "agent-instance-1",
        "step_id": "step-1",
        "tool_call_id": "tool-call-1",
        "attempt": 2,
        "sequence": 5,
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "tracestate": "vendor=value",
        "tool": "safe_shell",
        "arguments": {"command": "uname -s"},
    }
    data.update(overrides)
    return ToolRequest(**data)


def event_envelope(event: Event) -> dict:
    return {
        "run_id": event.run_id,
        "agent_id": event.agent_id,
        "agent_instance_id": event.agent_instance_id,
        "step_id": event.step_id,
        "tool_call_id": event.tool_call_id,
        "attempt": event.attempt,
        "sequence": event.sequence,
        "idempotency_key": event.idempotency_key,
        "traceparent": event.traceparent,
        "tracestate": event.tracestate,
        "trace_id": event.trace_id,
        "parent_id": event.parent_id,
    }


@pytest.mark.asyncio
async def test_worker_emits_started_and_heartbeat_with_request_envelope(monkeypatch) -> None:
    request = tool_request()
    worker = ToolWorker("safe_shell", heartbeat_interval_seconds=0)
    worker.nc = FakeNats()
    message = FakeMessage(request)

    async def succeed(_tool, _arguments, *, deadline_at, sandbox=None, run_id=None):
        return {"exit_code": 0, "stdout": "Linux", "stderr": ""}

    monkeypatch.setattr("swarmguard.tools.execute_async", succeed)

    await worker.handle(message)

    assert [event.kind for event in worker.nc.published] == [
        EventKind.TOOL_STARTED,
        EventKind.TOOL_HEARTBEAT,
    ]
    assert all(event_envelope(event) == event_envelope(request) for event in worker.nc.published)
    response = message.responses[0]
    assert response.allowed is True
    assert response.execution_status == ExecutionStatus.COMPLETED
    assert response.result == {"exit_code": 0, "stdout": "Linux", "stderr": ""}
    assert event_envelope(response) == event_envelope(request)


@pytest.mark.asyncio
async def test_worker_returns_failed_response_without_logical_terminal_event(monkeypatch) -> None:
    request = tool_request()
    worker = ToolWorker("safe_shell", heartbeat_interval_seconds=0)
    worker.nc = FakeNats()
    message = FakeMessage(request)

    async def fail(_tool, _arguments, *, deadline_at, sandbox=None, run_id=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("swarmguard.tools.execute_async", fail)

    await worker.handle(message)

    assert [event.kind for event in worker.nc.published] == [EventKind.TOOL_STARTED, EventKind.TOOL_HEARTBEAT]
    assert message.responses[0].execution_status == ExecutionStatus.FAILED
    assert message.responses[0].error == "boom"
    assert event_envelope(message.responses[0]) == event_envelope(request)


@pytest.mark.asyncio
async def test_worker_rejects_request_for_different_tool_before_started_event(monkeypatch) -> None:
    request = tool_request(tool="web_lookup", arguments={"url": "https://example.com"})
    worker = ToolWorker("safe_shell", heartbeat_interval_seconds=0)
    worker.nc = FakeNats()
    message = FakeMessage(request)
    called = False

    async def should_not_execute(_tool, _arguments, *, deadline_at, sandbox=None, run_id=None):
        nonlocal called
        called = True
        return {"exit_code": 0, "stdout": "Linux", "stderr": ""}

    monkeypatch.setattr("swarmguard.tools.execute_async", should_not_execute)

    await worker.handle(message)

    assert called is False
    assert worker.nc.published == []
    response = message.responses[0]
    assert response.allowed is False
    assert response.execution_status == ExecutionStatus.DENIED
    assert response.reason == "request tool web_lookup does not match worker tool safe_shell"


@pytest.mark.asyncio
async def test_worker_deadline_stops_execution_heartbeat_and_late_response(monkeypatch) -> None:
    request = tool_request(deadline_at=time.time() + 0.2, arguments={"command": "slow"})
    worker = ToolWorker("safe_shell", heartbeat_interval_seconds=0.01)
    worker.nc = FakeNats()
    message = FakeMessage(request)
    started = asyncio.Event()
    finished = asyncio.Event()

    async def slow_execute(_tool, _arguments, *, deadline_at, sandbox=None, run_id=None):
        started.set()
        await asyncio.sleep(1)
        finished.set()
        return {"stdout": "late"}

    monkeypatch.setattr("swarmguard.tools.execute_async", slow_execute)

    task = asyncio.create_task(worker.handle(message))
    await asyncio.wait_for(started.wait(), timeout=0.2)
    await eventually(lambda: any(event.kind == EventKind.TOOL_HEARTBEAT for event in worker.nc.published))
    await task
    event_count_at_terminal = len(worker.nc.published)
    await asyncio.sleep(0.04)

    assert message.responses == []
    assert len(worker.nc.published) == event_count_at_terminal
    assert not finished.is_set()


@pytest.mark.asyncio
async def test_worker_cancellation_stops_execution_heartbeat_and_late_response(monkeypatch) -> None:
    request = tool_request(arguments={"command": "slow"})
    worker = ToolWorker("safe_shell", heartbeat_interval_seconds=0.01)
    worker.nc = FakeNats()
    message = FakeMessage(request)
    cancelled = asyncio.Event()

    async def slow_execute(_tool, _arguments, *, deadline_at, sandbox=None, run_id=None):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr("swarmguard.tools.execute_async", slow_execute)

    task = asyncio.create_task(worker.handle(message))
    await eventually(lambda: any(event.kind == EventKind.TOOL_HEARTBEAT for event in worker.nc.published))
    assert await worker.cancel_tool_call(request) is True
    await task
    event_count_at_cancel = len(worker.nc.published)
    await asyncio.sleep(0.03)

    assert cancelled.is_set()
    assert message.responses == []
    assert len(worker.nc.published) == event_count_at_cancel


@pytest.mark.asyncio
async def test_sync_tool_timeout_terminates_and_reaps_child_process(monkeypatch, tmp_path) -> None:
    pid_file = tmp_path / "child.pid"

    def blocking_execute(_tool, _arguments, _run_id=None):
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(0.5)
        return {"late": True}

    monkeypatch.setattr("swarmguard.tools.execute", blocking_execute)

    with pytest.raises(asyncio.TimeoutError):
        await execute_async("workspace_read", {"path": "ignored"}, deadline_at=time.time() + 0.03)

    child_pid = int(pid_file.read_text(encoding="utf-8"))
    assert child_pid != os.getpid()
    await eventually(lambda: not pid_exists(child_pid), timeout=0.3)


@pytest.mark.asyncio
async def test_worker_sync_deadline_reaps_child_and_emits_no_late_response_or_heartbeat(monkeypatch, tmp_path) -> None:
    pid_file = tmp_path / "child.pid"
    request = tool_request(
        tool="workspace_read",
        arguments={"path": "ignored"},
        deadline_at=time.time() + 0.2,
    )
    worker = ToolWorker("workspace_read", heartbeat_interval_seconds=0.01)
    worker.nc = FakeNats()
    message = FakeMessage(request)

    def blocking_execute(_tool, _arguments, _run_id=None):
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(0.5)
        return {"late": True}

    monkeypatch.setattr("swarmguard.tools.execute", blocking_execute)
    # This test forks a real child under pytest; skip applying rlimits so the
    # large test-runner address space is not capped. Sandbox behavior itself is
    # covered directly in tests/test_tool_sandbox.py.
    monkeypatch.setattr("swarmguard.tools._apply_sandbox", lambda sandbox: None)

    task = asyncio.create_task(worker.handle(message))
    await eventually(lambda: pid_file.exists(), timeout=0.2)
    await eventually(lambda: any(event.kind == EventKind.TOOL_HEARTBEAT for event in worker.nc.published))
    await task
    event_count_at_timeout = len(worker.nc.published)
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    await asyncio.sleep(0.04)

    assert child_pid != os.getpid()
    assert not pid_exists(child_pid)
    assert message.responses == []
    assert len(worker.nc.published) == event_count_at_timeout


class RawMessage:
    def __init__(self, data: bytes) -> None:
        self.data = data


@pytest.mark.asyncio
async def test_worker_cancels_only_exact_identity_when_tool_call_id_collides(monkeypatch) -> None:
    worker = ToolWorker("safe_shell", heartbeat_interval_seconds=0)
    worker.nc = FakeNats()
    started: set[str] = set()
    cancelled: set[str] = set()

    async def controllable(_tool, arguments, *, deadline_at, sandbox=None, run_id=None):
        key = str(arguments["command"])
        started.add(key)
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.add(key)
            raise

    monkeypatch.setattr("swarmguard.tools.execute_async", controllable)

    # Two active calls that share tool_call_id but differ by run/agent/instance/attempt.
    target = tool_request(
        run_id="run-A", agent_id="researcher", agent_instance_id="inst-A", attempt=1, arguments={"command": "A"}
    )
    bystander = tool_request(
        run_id="run-B", agent_id="analyst", agent_instance_id="inst-B", attempt=3, arguments={"command": "B"}
    )
    assert target.tool_call_id == bystander.tool_call_id == "tool-call-1"

    task_target = asyncio.create_task(worker.handle(FakeMessage(target)))
    task_bystander = asyncio.create_task(worker.handle(FakeMessage(bystander)))
    await eventually(lambda: {"A", "B"} <= started)

    try:
        # A colliding tool_call_id from an unrelated identity must cancel nothing.
        stranger = tool_request(
            run_id="run-Z", agent_id="operator", agent_instance_id="inst-Z", attempt=9, arguments={"command": "A"}
        )
        assert await worker.cancel_tool_call(stranger) is False
        await asyncio.sleep(0.02)
        assert cancelled == set()
        assert not task_target.done()
        assert not task_bystander.done()

        # Exact canonical identity cancels only the target.
        assert await worker.cancel_tool_call(target) is True
        await task_target
        await asyncio.sleep(0.02)

        assert cancelled == {"A"}
        assert not task_bystander.done()
    finally:
        task_bystander.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task_bystander


@pytest.mark.asyncio
async def test_worker_handle_cancel_ignores_malformed_wrong_tool_and_mismatched_payloads(monkeypatch) -> None:
    worker = ToolWorker("safe_shell", heartbeat_interval_seconds=0)
    worker.nc = FakeNats()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def controllable(_tool, _arguments, *, deadline_at, sandbox=None, run_id=None):
        started.set()
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr("swarmguard.tools.execute_async", controllable)

    request = tool_request()
    task = asyncio.create_task(worker.handle(FakeMessage(request)))
    await started.wait()

    try:
        # Malformed payload: must be ignored without raising.
        await worker.handle_cancel(RawMessage(b"not-json"))
        # Wrong tool routed here by mistake: ignored.
        await worker.handle_cancel(FakeMessage(tool_request(tool="web_lookup")))
        # Same tool_call_id but different run/agent: identity mismatch, ignored.
        await worker.handle_cancel(FakeMessage(tool_request(run_id="run-other")))
        await worker.handle_cancel(FakeMessage(tool_request(agent_id="intruder")))
        await asyncio.sleep(0.02)

        assert not cancelled.is_set()
        assert not task.done()

        # Exact identity via handle_cancel cancels the active call.
        await worker.handle_cancel(FakeMessage(request))
        await task
        assert cancelled.is_set()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_worker_rejects_input_that_does_not_match_manifest_schema(monkeypatch) -> None:
    request = tool_request(arguments={})
    worker = ToolWorker("safe_shell", heartbeat_interval_seconds=0)
    worker.nc = FakeNats()
    message = FakeMessage(request)
    called = False

    async def should_not_execute(_tool, _arguments, *, deadline_at, sandbox=None, run_id=None):
        nonlocal called
        called = True
        return {"exit_code": 0, "stdout": "Linux", "stderr": ""}

    monkeypatch.setattr("swarmguard.tools.execute_async", should_not_execute)

    await worker.handle(message)

    response = message.responses[0]
    assert called is False
    assert response.execution_status == ExecutionStatus.FAILED
    assert "input schema" in str(response.error)


@pytest.mark.asyncio
async def test_worker_rejects_success_output_that_does_not_match_manifest_schema(monkeypatch) -> None:
    request = tool_request()
    worker = ToolWorker("safe_shell", heartbeat_interval_seconds=0)
    worker.nc = FakeNats()
    message = FakeMessage(request)

    async def invalid_output(_tool, _arguments, *, deadline_at, sandbox=None, run_id=None):
        return {"stdout": "Linux"}

    monkeypatch.setattr("swarmguard.tools.execute_async", invalid_output)

    await worker.handle(message)

    response = message.responses[0]
    assert response.execution_status == ExecutionStatus.FAILED
    assert "output schema" in str(response.error)


@pytest.mark.asyncio
async def test_extension_tool_executes_through_manifest() -> None:
    result = await execute_async("echo_metadata", {"message": "hello"}, deadline_at=time.time() + 1)

    assert result == {"echo": "hello", "length": 5}
