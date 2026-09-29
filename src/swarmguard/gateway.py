from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from nats.aio.msg import Msg
from pydantic import ValidationError

from .bus import connect, publish_event, wait_for_shutdown
from .harness import decode_signed_harness_record, harness_record_to_event
from .otel import start_tool_attempt_span, start_tool_call_span
from .policy import Decision, PolicyEngine
from .protocol import (
    Event,
    EventKind,
    ExecutionStatus,
    PolicyDecision,
    ToolRequest,
    ToolResponse,
    parse_agent_message_subject,
    parse_agent_lifecycle_subject,
    parse_agent_tool_subject,
    tool_cancel_subject,
    tool_execute_subject,
)
from .registry import AgentRegistry, ConfigError, ToolManifest, ToolRegistry, load_agent_registry, load_tool_registry


EventPublisher = Callable[[Event], Awaitable[None]]
# Agent-facing text is deliberately playful for the validation experiment only;
# operators and alerting key off the structured reason code, never this string.
DECOY_ERROR = "LoL you got scammed"
DECOY_REASON_CODE = "decoy_tool_invoked"


class WorkerClient(Protocol):
    async def request(self, tool: str, request: ToolRequest, timeout: float) -> ToolResponse:
        pass


class CancellationSender(Protocol):
    async def cancel(self, request: ToolRequest) -> None:
        pass


class NoopCancellationSender:
    async def cancel(self, request: ToolRequest) -> None:
        return None


@dataclass(frozen=True)
class GatewayConfig:
    tool_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("TOOL_TIMEOUT_SECONDS", "20")))
    max_attempts: int = field(default_factory=lambda: int(os.getenv("TOOL_MAX_ATTEMPTS", "2")))
    request_deadline_seconds: float | None = field(
        default_factory=lambda: (
            float(os.environ["TOOL_REQUEST_DEADLINE_SECONDS"])
            if os.getenv("TOOL_REQUEST_DEADLINE_SECONDS")
            else None
        )
    )
    dead_letter_prefix: str = field(default_factory=lambda: os.getenv("TOOL_DEAD_LETTER_PREFIX", "deadletter"))
    tool_registry: ToolRegistry = field(default_factory=load_tool_registry)

    def worker_subject(self, tool: str) -> str:
        try:
            return self.tool_registry.resolve_tool(tool).worker_subject
        except ConfigError:
            return tool_execute_subject(tool)

    def dead_letter_subject(self, run_id: str) -> str:
        return f"{self.dead_letter_prefix}.{run_id}.tool"


@dataclass
class IdempotencyRecord:
    fingerprint: str
    task: asyncio.Task[ToolResponse]
    cancelled: bool = False
    active_request: ToolRequest | None = None


class NatsWorkerClient:
    def __init__(self, nc, config: GatewayConfig):
        self.nc = nc
        self.config = config

    async def request(self, tool: str, request: ToolRequest, timeout: float) -> ToolResponse:
        reply = await self.nc.request(
            self.config.worker_subject(tool),
            request.model_dump_json().encode(),
            timeout=timeout,
        )
        return ToolResponse.model_validate_json(reply.data)


class NatsCancellationSender:
    def __init__(self, nc):
        self.nc = nc

    async def cancel(self, request: ToolRequest) -> None:
        # Routed tool scope in the subject, full canonical identity in the payload.
        await self.nc.publish(
            tool_cancel_subject(request.tool, request.tool_call_id or request.request_id),
            request.model_dump_json().encode(),
        )


class ExecutionCoordinator:
    def __init__(
        self,
        *,
        policy: PolicyEngine,
        worker_client: WorkerClient,
        event_publisher: EventPublisher,
        config: GatewayConfig | None = None,
        monotonic: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        cancellation_sender: CancellationSender | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_manifest_path: str | Path | None = None,
        agent_registry: AgentRegistry | None = None,
    ):
        self.policy = policy
        self.worker_client = worker_client
        self.publish_event = event_publisher
        self.config = config or GatewayConfig()
        self.monotonic = monotonic or time.monotonic
        self.wall_clock = wall_clock or time.time
        self.cancellation_sender = cancellation_sender or NoopCancellationSender()
        self.tool_registry = tool_registry or (
            load_tool_registry(tool_manifest_path) if tool_manifest_path is not None else self.config.tool_registry
        )
        self.agent_registry = agent_registry or load_agent_registry()
        self._idempotency: dict[str, IdempotencyRecord] = {}
        self._client_idempotency: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def handle_tool_request(self, subject: str, request: ToolRequest) -> ToolResponse:
        try:
            run_id, agent_id = parse_agent_tool_subject(subject)
        except ValueError as exc:
            return self._rejected(request, str(exc))
        if request.run_id != run_id:
            return self._rejected(request, "run_id does not match authenticated subject namespace")
        if request.agent_id is not None and request.agent_id != agent_id:
            return self._rejected(request, "agent_id does not match authenticated subject namespace")

        client_key = request.idempotency_key or ""
        fingerprint = _request_fingerprint(agent_id, request)
        key = _gateway_idempotency_key(fingerprint)
        request = request.model_copy(update={"agent_id": agent_id, "idempotency_key": key})
        async with self._lock:
            if client_key:
                existing_key = self._client_idempotency.get(client_key)
                if existing_key is not None and existing_key != key:
                    return self._rejected(request, "idempotency key conflict")
                self._client_idempotency[client_key] = key
            record = self._idempotency.get(key)
            if record is not None:
                if record.fingerprint != fingerprint:
                    return self._rejected(request, "idempotency key conflict")
                task = record.task
            else:
                task = asyncio.create_task(self._execute(run_id, agent_id, request))
                self._idempotency[key] = IdempotencyRecord(fingerprint=fingerprint, task=task)

        return await task

    def cancel(self, idempotency_key: str) -> bool:
        record = self._idempotency.get(self._client_idempotency.get(idempotency_key, idempotency_key))
        if record is None or record.task.done():
            return False
        record.cancelled = True
        if record.active_request is not None:
            asyncio.create_task(self._send_cancellation(record.active_request))
        return True

    async def _execute(self, run_id: str, agent_id: str, request: ToolRequest) -> ToolResponse:
        with start_tool_call_span(run_id, request.tool_call_id or request.request_id, tool_name=request.tool, traceparent=request.traceparent):
            return await self._execute_with_span(run_id, agent_id, request)

    async def _execute_with_span(self, run_id: str, agent_id: str, request: ToolRequest) -> ToolResponse:
        deadline = self._deadline()
        deadline_at = self._deadline_at(deadline)
        await self._publish(
            request,
            agent_id,
            EventKind.TOOL_REQUESTED,
            state="requested",
            extra={"arguments": request.arguments},
        )
        try:
            manifest = self.tool_registry.resolve_tool(request.tool)
        except ConfigError as exc:
            response = self._denied(request, str(exc))
            await self._publish_terminal(request, agent_id, EventKind.TOOL_DENIED, "denied", response)
            return response
        if manifest.classification == "decoy" or request.tool in self._decoy_tools(agent_id):
            response = self._denied(request, f"decoy tool invoked: {request.tool}")
            response = response.model_copy(update={"error": DECOY_ERROR, "reason_code": DECOY_REASON_CODE})
            await self._publish_terminal(request, agent_id, EventKind.TOOL_DENIED, "denied", response)
            await self._publish(
                request,
                agent_id,
                EventKind.SECURITY_DECOY_TRIGGERED,
                state="denied",
                extra={"reason_code": DECOY_REASON_CODE, "severity": "high"},
            )
            return response
        if not manifest.dispatchable:
            response = self._denied(request, f"tool manifest is not dispatchable: {request.tool}")
            await self._publish_terminal(request, agent_id, EventKind.TOOL_DENIED, "denied", response)
            return response

        decision = self.policy.evaluate(agent_id, request.tool, request.arguments)
        await self._publish_policy_decision(request, agent_id, decision)
        if not decision.allowed:
            response = self._denied(request, decision.reason)
            await self._publish_terminal(request, agent_id, EventKind.TOOL_DENIED, "denied", response)
            return response

        response: ToolResponse | None = None
        attempts = max(1, min(self.config.max_attempts, manifest.retry_policy.max_attempts))
        for attempt in range(1, attempts + 1):
            attempt_request = _request_for_attempt(request, attempt)
            with start_tool_attempt_span(run_id, attempt_request.tool_call_id or attempt_request.request_id, attempt, tool_name=attempt_request.tool, traceparent=attempt_request.traceparent):
                attempt_request = attempt_request.model_copy(update={"deadline_at": deadline_at})
                attempt_request = self._prepare_worker_request(attempt_request)
                attempt_timeout = self._attempt_timeout(deadline, manifest)
                if attempt_timeout <= 0:
                    response = ToolResponse.timed_out(
                        request_id=attempt_request.request_id,
                        reason="tool request deadline exhausted",
                        **_response_envelope(attempt_request),
                    )
                    await self._publish_terminal(attempt_request, agent_id, EventKind.TOOL_TIMED_OUT, "timed_out", response)
                    await self._publish(attempt_request, agent_id, EventKind.TOOL_DEAD_LETTER, state="dead_letter")
                    return response
                await self._publish(attempt_request, agent_id, EventKind.TOOL_DISPATCHED, state="dispatched")
                if self._is_cancelled(request):
                    response = self._cancelled(attempt_request, "tool request cancelled")
                    await self._send_cancellation(attempt_request)
                    await self._publish_terminal(attempt_request, agent_id, EventKind.TOOL_CANCELLED, "cancelled", response)
                    return response
                try:
                    self._set_active_request(request, attempt_request)
                    worker_response = await self.worker_client.request(
                        attempt_request.tool,
                        attempt_request,
                        timeout=attempt_timeout,
                    )
                    response = self._response_from_worker(attempt_request, worker_response, decision.reason)
                except asyncio.TimeoutError:
                    response = ToolResponse.timed_out(
                        request_id=attempt_request.request_id,
                        reason="tool worker timed out",
                        **_response_envelope(attempt_request),
                    )
                    await self._send_cancellation(attempt_request)
                except Exception as exc:
                    response = ToolResponse(
                        request_id=attempt_request.request_id,
                        allowed=True,
                        policy_decision=PolicyDecision.ALLOWED,
                        execution_status=ExecutionStatus.FAILED,
                        error=str(exc),
                        reason=decision.reason,
                        **_response_envelope(attempt_request),
                    )
                finally:
                    self._clear_active_request(request, attempt_request)

                if self._is_cancelled(request):
                    response = self._cancelled(attempt_request, "tool request cancelled")
                    await self._send_cancellation(attempt_request)
                    await self._publish_terminal(attempt_request, agent_id, EventKind.TOOL_CANCELLED, "cancelled", response)
                    return response
                if response.execution_status == ExecutionStatus.COMPLETED:
                    await self._publish_terminal(attempt_request, agent_id, EventKind.TOOL_COMPLETED, "completed", response)
                    return response
                if (
                    attempt < attempts
                    and response.execution_status in {ExecutionStatus.TIMED_OUT, ExecutionStatus.FAILED}
                    and self._attempt_timeout(deadline, manifest) > 0
                ):
                    continue

                kind, state = _terminal_event_for_status(response.execution_status)
                await self._publish_terminal(attempt_request, agent_id, kind, state, response)
                await self._publish(attempt_request, agent_id, EventKind.TOOL_DEAD_LETTER, state="dead_letter")
                return response

        assert response is not None
        return response

    def _decoy_tools(self, agent_id: str) -> tuple[str, ...]:
        try:
            return self.agent_registry.resolve_agent(agent_id).decoy_tools
        except ConfigError:
            return ()

    def _deadline(self) -> float | None:
        if self.config.request_deadline_seconds is None:
            return None
        return self.monotonic() + self.config.request_deadline_seconds

    def _deadline_at(self, deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return self.wall_clock() + max(0.0, deadline - self.monotonic())

    def _attempt_timeout(self, deadline: float | None, manifest: ToolManifest) -> float:
        configured = min(self.config.tool_timeout_seconds, manifest.timeout_seconds)
        if deadline is None:
            return configured
        return min(configured, max(0.0, deadline - self.monotonic()))

    def _set_active_request(self, logical_request: ToolRequest, attempt_request: ToolRequest) -> None:
        record = self._idempotency.get(logical_request.idempotency_key or "")
        if record is not None:
            record.active_request = attempt_request

    def _clear_active_request(self, logical_request: ToolRequest, attempt_request: ToolRequest) -> None:
        record = self._idempotency.get(logical_request.idempotency_key or "")
        if record is not None and record.active_request == attempt_request:
            record.active_request = None

    async def _send_cancellation(self, request: ToolRequest) -> None:
        await self.cancellation_sender.cancel(request)

    def _prepare_worker_request(self, request: ToolRequest) -> ToolRequest:
        if request.tool != "workspace_read":
            return request
        arguments = dict(request.arguments)
        arguments["path"] = str(Path(str(arguments["path"])).resolve())
        return request.model_copy(update={"arguments": arguments})

    def _response_from_worker(self, request: ToolRequest, response: ToolResponse, reason: str) -> ToolResponse:
        return ToolResponse(
            request_id=request.request_id,
            allowed=True,
            policy_decision=PolicyDecision.ALLOWED,
            execution_status=response.execution_status,
            result=response.result,
            error=response.error,
            reason=response.reason or reason,
            **_response_envelope(request),
        )

    def _is_cancelled(self, request: ToolRequest) -> bool:
        record = self._idempotency.get(request.idempotency_key or "")
        return bool(record and record.cancelled)

    def _denied(self, request: ToolRequest, reason: str) -> ToolResponse:
        return ToolResponse(
            request_id=request.request_id,
            allowed=False,
            policy_decision=PolicyDecision.DENIED,
            execution_status=ExecutionStatus.DENIED,
            error="policy denied request",
            reason=reason,
            **_response_envelope(request),
        )

    def _cancelled(self, request: ToolRequest, reason: str) -> ToolResponse:
        return ToolResponse(
            request_id=request.request_id,
            allowed=True,
            policy_decision=PolicyDecision.ALLOWED,
            execution_status=ExecutionStatus.CANCELLED,
            error=reason,
            reason=reason,
            **_response_envelope(request),
        )

    def _rejected(self, request: ToolRequest, reason: str) -> ToolResponse:
        return ToolResponse(
            request_id=request.request_id,
            allowed=False,
            policy_decision=PolicyDecision.DENIED,
            execution_status=ExecutionStatus.DENIED,
            error=reason,
            reason=reason,
            **_response_envelope(request),
        )

    async def _publish_policy_decision(self, request: ToolRequest, agent_id: str, decision: Decision) -> None:
        decision_text = "allowed" if decision.allowed else "denied"
        await self._publish(
            request,
            agent_id,
            EventKind.TOOL_POLICY_DECIDED,
            state="policy_decided",
            extra={"decision": decision_text, "reason": decision.reason},
        )
        await self._publish(
            request,
            agent_id,
            EventKind.TOOL_ALLOWED if decision.allowed else EventKind.TOOL_DENIED,
            state=decision_text,
            extra={
                "decision": decision_text,
                "reason": decision.reason,
                "compatibility": "legacy_policy_decision",
            },
        )

    async def _publish_terminal(
        self,
        request: ToolRequest,
        agent_id: str,
        kind: EventKind,
        state: str,
        response: ToolResponse,
    ) -> None:
        payload: dict[str, object] = {
            "status": response.execution_status,
            "state": state,
            "ok": response.execution_status == ExecutionStatus.COMPLETED,
        }
        if response.error:
            payload["error"] = response.error
        if response.reason_code:
            payload["reason_code"] = response.reason_code
        await self._publish(request, agent_id, kind, state=state, extra=payload)

    async def _publish(
        self,
        request: ToolRequest,
        agent_id: str,
        kind: EventKind,
        *,
        state: str,
        extra: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "request_id": request.request_id,
            "tool": request.tool,
            "state": state,
        }
        if extra:
            payload.update(extra)
        await self.publish_event(
            Event(
                run_id=request.run_id,
                agent_id=agent_id,
                kind=kind,
                payload=payload,
                **_event_envelope(request),
            )
        )


class PolicyGateway:
    def __init__(self, policy: PolicyEngine, config: GatewayConfig | None = None):
        self.policy = policy
        self.config = config or GatewayConfig()
        self.nc = None
        self.coordinator: ExecutionCoordinator | None = None

    async def start(self) -> None:
        self.nc = await connect("policy-gateway")
        self.coordinator = ExecutionCoordinator(
            policy=self.policy,
            worker_client=NatsWorkerClient(self.nc, self.config),
            event_publisher=self._publish_gateway_event,
            config=self.config,
            cancellation_sender=NatsCancellationSender(self.nc),
        )
        await self.nc.subscribe("swarm.*.agent.*.tool.request", cb=self.handle)
        await self.nc.subscribe("swarm.*.agent.*.message.*", cb=self.handle_message)
        await self.nc.subscribe("swarm.*.agent.*.lifecycle", cb=self.handle_lifecycle)

    async def _publish_gateway_event(self, event: Event) -> None:
        assert self.nc is not None
        await publish_event(self.nc, event)
        if event.kind == EventKind.TOOL_DEAD_LETTER:
            await self.nc.publish(self.config.dead_letter_subject(event.run_id), event.model_dump_json().encode())

    async def handle_message(self, msg: Msg) -> None:
        assert self.nc is not None
        try:
            run_id, sender, recipient = parse_agent_message_subject(msg.subject)
            body = json.loads(msg.data)
            if body.get("run_id") != run_id or body.get("sender") != sender or body.get("recipient") != recipient:
                return
            text = str(body.get("text", ""))[:2_000]
        except (ValueError, TypeError):
            return
        await publish_event(
            self.nc,
            Event(
                run_id=run_id,
                agent_id=sender,
                kind=EventKind.A2A_SENT,
                payload={"recipient": recipient, "text": text},
            ),
        )

    async def handle_lifecycle(self, msg: Msg) -> None:
        assert self.nc is not None
        try:
            run_id, agent_id = parse_agent_lifecycle_subject(msg.subject)
            record = decode_signed_harness_record(msg.data)
        except (ValueError, TypeError, json.JSONDecodeError, KeyError):
            return
        if record.run_id != run_id or record.agent_id != agent_id:
            return
        event = harness_record_to_event(record)
        if event is None:
            return
        await publish_event(self.nc, event)

    async def handle(self, msg: Msg) -> None:
        assert self.nc is not None
        try:
            request = ToolRequest.model_validate_json(msg.data)
        except (ValueError, ValidationError) as exc:
            await msg.respond(
                ToolResponse(request_id="invalid", allowed=False, error=str(exc), reason="invalid request")
                .model_dump_json()
                .encode()
            )
            return

        if self.coordinator is None:
            self.coordinator = ExecutionCoordinator(
                policy=self.policy,
                worker_client=NatsWorkerClient(self.nc, self.config),
                event_publisher=self._publish_gateway_event,
                config=self.config,
                cancellation_sender=NatsCancellationSender(self.nc),
            )
        response = await self.coordinator.handle_tool_request(msg.subject, request)
        await msg.respond(response.model_dump_json().encode())


def _event_envelope(request: ToolRequest) -> dict[str, object | None]:
    return {
        "agent_instance_id": request.agent_instance_id,
        "step_id": request.step_id,
        "tool_call_id": request.tool_call_id,
        "attempt": request.attempt,
        "sequence": request.sequence,
        "idempotency_key": request.idempotency_key,
        "traceparent": request.traceparent,
        "tracestate": request.tracestate,
        "trace_id": request.trace_id,
        "parent_id": request.parent_id,
        "deadline_at": request.deadline_at,
    }


def _response_envelope(request: ToolRequest) -> dict[str, object | None]:
    return {
        "run_id": request.run_id,
        "agent_id": request.agent_id,
        **_event_envelope(request),
    }


def _request_for_attempt(request: ToolRequest, attempt: int) -> ToolRequest:
    if request.attempt == attempt:
        return request
    return request.model_copy(update={"attempt": attempt})


def _request_fingerprint(agent_id: str, request: ToolRequest) -> str:
    body = {
        "agent_id": agent_id,
        "run_id": request.run_id,
        "agent_instance_id": request.agent_instance_id,
        "step_id": request.step_id,
        "tool_call_id": request.tool_call_id,
        "tool": request.tool,
        "arguments": request.arguments,
        "traceparent": request.traceparent,
        "tracestate": request.tracestate,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def _gateway_idempotency_key(fingerprint: str) -> str:
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _terminal_event_for_status(status: ExecutionStatus | None) -> tuple[EventKind, str]:
    if status == ExecutionStatus.TIMED_OUT:
        return EventKind.TOOL_TIMED_OUT, "timed_out"
    if status == ExecutionStatus.CANCELLED:
        return EventKind.TOOL_CANCELLED, "cancelled"
    if status == ExecutionStatus.DENIED:
        return EventKind.TOOL_DENIED, "denied"
    if status == ExecutionStatus.COMPLETED:
        return EventKind.TOOL_COMPLETED, "completed"
    return EventKind.TOOL_FAILED, "failed"


async def run(policy_path: str) -> None:
    policy = PolicyEngine(policy_path)
    gateway = PolicyGateway(policy)
    # `kill -HUP <gateway>` reloads policy; a malformed file raises before the swap, keeping the old rules.
    asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, policy.reload)
    await gateway.start()
    assert gateway.nc is not None
    try:
        await wait_for_shutdown()
    finally:
        await gateway.nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", default=os.getenv("POLICY_FILE", "config/policies.yaml"))
    args = parser.parse_args()
    asyncio.run(run(args.policy))


if __name__ == "__main__":
    main()
