from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hmac
import json
from typing import Any, Literal, Protocol
from uuid import uuid4

from .otel import instrument_harness_record
from .protocol import Event, EventKind
from .security import resolve_harness_signing_key


LifecycleStatus = Literal["started", "completed", "failed", "cancelled", "exhausted"]


@dataclass(frozen=True)
class HarnessRecord:
    kind: str
    run_id: str
    agent_id: str | None = None
    agent_instance_id: str | None = None
    step_id: str | None = None
    tool_call_id: str | None = None
    traceparent: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    record_id: str = field(default_factory=lambda: str(uuid4()))
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class HarnessAdapter(Protocol):
    def run_started(self, *, run_id: str, agent_id: str) -> HarnessRecord: ...

    def run_ended(
        self,
        *,
        run_id: str,
        agent_id: str,
        status: LifecycleStatus,
        error: str | None = None,
    ) -> HarnessRecord: ...

    def agent_session_started(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_instance_id: str,
    ) -> HarnessRecord: ...

    def agent_session_ended(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_instance_id: str,
        status: LifecycleStatus,
        error: str | None = None,
    ) -> HarnessRecord: ...

    def model_step_started(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_instance_id: str,
        step_id: str,
    ) -> HarnessRecord: ...

    def model_step_ended(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_instance_id: str,
        step_id: str,
        status: LifecycleStatus,
        error: str | None = None,
    ) -> HarnessRecord: ...

    def handoff(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_instance_id: str,
        step_id: str | None,
        recipient: str,
        message: str,
    ) -> HarnessRecord: ...

    def tool_intent(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_instance_id: str,
        step_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        traceparent: str | None = None,
    ) -> HarnessRecord: ...


class InMemoryHarnessRecorder:
    def __init__(self, records: list[HarnessRecord] | None = None):
        self.records: list[HarnessRecord] = records if records is not None else []
        self._records_by_id = {record.record_id: record for record in self.records}
        self._records_by_logical_key = {_logical_key(record): record for record in self.records}
        self._next_sequence = max((record.sequence for record in self.records), default=0) + 1

    def record(self, record: HarnessRecord) -> HarnessRecord:
        instrument_harness_record(
            kind=record.kind,
            run_id=record.run_id,
            agent_id=record.agent_id,
            agent_instance_id=record.agent_instance_id,
            step_id=record.step_id,
            tool_call_id=record.tool_call_id,
            traceparent=record.traceparent,
        )
        existing = self._records_by_id.get(record.record_id)
        if existing is not None:
            return existing
        logical_key = _logical_key(record)
        existing = self._records_by_logical_key.get(logical_key)
        if existing is not None:
            return existing

        stored = replace(record, sequence=self._next_sequence)
        self._next_sequence += 1
        self.records.append(stored)
        self._records_by_id[stored.record_id] = stored
        self._records_by_logical_key[logical_key] = stored
        return stored

    def run_started(self, *, run_id: str, agent_id: str) -> HarnessRecord:
        return self.record(HarnessRecord(kind="run.started", run_id=run_id, agent_id=agent_id))

    def run_ended(
        self,
        *,
        run_id: str,
        agent_id: str,
        status: LifecycleStatus,
        error: str | None = None,
    ) -> HarnessRecord:
        return self.record(
            HarnessRecord(kind="run.ended", run_id=run_id, agent_id=agent_id, payload=_status_payload(status, error))
        )

    def agent_session_started(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_instance_id: str,
    ) -> HarnessRecord:
        return self.record(
            HarnessRecord(
                kind="agent_session.started",
                run_id=run_id,
                agent_id=agent_id,
                agent_instance_id=agent_instance_id,
            )
        )

    def agent_session_ended(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_instance_id: str,
        status: LifecycleStatus,
        error: str | None = None,
    ) -> HarnessRecord:
        return self.record(
            HarnessRecord(
                kind="agent_session.ended",
                run_id=run_id,
                agent_id=agent_id,
                agent_instance_id=agent_instance_id,
                payload=_status_payload(status, error),
            )
        )

    def model_step_started(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_instance_id: str,
        step_id: str,
    ) -> HarnessRecord:
        return self.record(
            HarnessRecord(
                kind="model_step.started",
                run_id=run_id,
                agent_id=agent_id,
                agent_instance_id=agent_instance_id,
                step_id=step_id,
            )
        )

    def model_step_ended(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_instance_id: str,
        step_id: str,
        status: LifecycleStatus,
        error: str | None = None,
    ) -> HarnessRecord:
        return self.record(
            HarnessRecord(
                kind="model_step.ended",
                run_id=run_id,
                agent_id=agent_id,
                agent_instance_id=agent_instance_id,
                step_id=step_id,
                payload=_status_payload(status, error),
            )
        )

    def handoff(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_instance_id: str,
        step_id: str | None,
        recipient: str,
        message: str,
    ) -> HarnessRecord:
        return self.record(
            HarnessRecord(
                kind="handoff",
                run_id=run_id,
                agent_id=agent_id,
                agent_instance_id=agent_instance_id,
                step_id=step_id,
                payload={"recipient": recipient, "message": message},
            )
        )

    def tool_intent(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_instance_id: str,
        step_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        traceparent: str | None = None,
    ) -> HarnessRecord:
        return self.record(
            HarnessRecord(
                kind="tool.intent",
                run_id=run_id,
                agent_id=agent_id,
                agent_instance_id=agent_instance_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                traceparent=traceparent,
                payload={"tool_name": tool_name, "arguments": arguments},
            )
        )


def _status_payload(status: LifecycleStatus, error: str | None) -> dict[str, str]:
    payload = {"status": status}
    if error is not None:
        payload["error"] = error
    return payload


def _logical_key(record: HarnessRecord) -> tuple[str, str, str | None, str | None, str | None, str | None, str]:
    return (
        record.kind,
        record.run_id,
        record.agent_id,
        record.agent_instance_id,
        record.step_id,
        record.tool_call_id,
        json.dumps(record.payload, sort_keys=True, separators=(",", ":"), default=str),
    )


def encode_signed_harness_record(record: HarnessRecord, *, secret: str | None = None) -> bytes:
    body = _record_body(record)
    return json.dumps(
        {
            "record": body,
            "signature": _signature(body, secret=secret),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()


def decode_signed_harness_record(data: bytes, *, secret: str | None = None) -> HarnessRecord:
    envelope = json.loads(data)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("record"), dict):
        raise ValueError("invalid signed harness record")
    record_body = envelope["record"]
    expected = _signature(record_body, secret=secret)
    if not hmac.compare_digest(str(envelope.get("signature", "")), expected):
        raise ValueError("invalid harness record signature")
    return HarnessRecord(
        kind=str(record_body["kind"]),
        run_id=str(record_body["run_id"]),
        agent_id=record_body.get("agent_id"),
        agent_instance_id=record_body.get("agent_instance_id"),
        step_id=record_body.get("step_id"),
        tool_call_id=record_body.get("tool_call_id"),
        traceparent=record_body.get("traceparent"),
        payload=dict(record_body.get("payload") or {}),
        record_id=str(record_body["record_id"]),
        sequence=int(record_body["sequence"]),
        timestamp=datetime.fromisoformat(str(record_body["timestamp"])),
    )


def harness_record_to_event(record: HarnessRecord) -> Event | None:
    kind = _harness_event_kind(record)
    if kind is None:
        return None
    payload = dict(record.payload)
    if kind != EventKind.HANDOFF:
        payload["state"] = payload.get("status", "active")
    return Event(
        event_id=f"harness:{record.record_id}",
        run_id=record.run_id,
        agent_id=record.agent_id,
        agent_instance_id=record.agent_instance_id,
        step_id=record.step_id,
        tool_call_id=record.tool_call_id,
        sequence=record.sequence,
        traceparent=record.traceparent,
        timestamp=record.timestamp,
        kind=kind,
        payload=payload,
    )


def _record_body(record: HarnessRecord) -> dict[str, Any]:
    return {
        "kind": record.kind,
        "run_id": record.run_id,
        "agent_id": record.agent_id,
        "agent_instance_id": record.agent_instance_id,
        "step_id": record.step_id,
        "tool_call_id": record.tool_call_id,
        "traceparent": record.traceparent,
        "payload": record.payload,
        "record_id": record.record_id,
        "sequence": record.sequence,
        "timestamp": record.timestamp.isoformat(),
    }


def _signature(record_body: dict[str, Any], *, secret: str | None) -> str:
    key = (secret or resolve_harness_signing_key()).encode()
    body = json.dumps(record_body, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hmac.new(key, body, "sha256").hexdigest()


def _harness_event_kind(record: HarnessRecord) -> EventKind | None:
    if record.kind == "run.started":
        return EventKind.RUN_STARTED
    if record.kind == "agent_session.started":
        return EventKind.AGENT_SESSION_STARTED
    if record.kind == "model_step.started":
        return EventKind.MODEL_STEP_STARTED
    if record.kind == "handoff":
        return EventKind.HANDOFF
    if record.kind == "artifact":
        return EventKind.ARTIFACT_CREATED
    status = record.payload.get("status")
    if record.kind == "run.ended":
        return {
            "completed": EventKind.RUN_COMPLETED,
            "failed": EventKind.RUN_FAILED,
            "cancelled": EventKind.RUN_CANCELLED,
            "exhausted": EventKind.RUN_EXHAUSTED,
        }.get(status)
    if record.kind == "agent_session.ended":
        return {
            "completed": EventKind.AGENT_SESSION_COMPLETED,
            "failed": EventKind.AGENT_SESSION_FAILED,
            "cancelled": EventKind.AGENT_SESSION_CANCELLED,
            "exhausted": EventKind.AGENT_SESSION_EXHAUSTED,
        }.get(status)
    if record.kind == "model_step.ended":
        return {
            "completed": EventKind.MODEL_STEP_COMPLETED,
            "failed": EventKind.MODEL_STEP_FAILED,
            "cancelled": EventKind.MODEL_STEP_CANCELLED,
            "exhausted": EventKind.MODEL_STEP_EXHAUSTED,
        }.get(status)
    return None
