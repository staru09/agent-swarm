from __future__ import annotations

import argparse
import asyncio
import json
import os
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.errors import FetchTimeoutError
from pydantic import ValidationError

from .bus import connect
from .protocol import Event, EventKind
from .streams import DURABLE_PROJECTOR_CONSUMER


TERMINAL_STATES = {"completed", "failed", "denied", "timed_out", "cancelled", "exhausted"}
STATE_RANK = {
    "active": 0,
    "requested": 10,
    "policy_decided": 20,
    "allowed": 25,
    "dispatched": 30,
    "started": 40,
    "heartbeat": 45,
    "completed": 100,
    "failed": 100,
    "denied": 100,
    "timed_out": 100,
    "cancelled": 100,
    "exhausted": 100,
    "dead_letter": 90,
}


class ProjectionTransaction(Protocol):
    async def insert_event(self, event: Event) -> bool: ...
    async def project_event(self, event: Event) -> None: ...


class ProjectionStore(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[ProjectionTransaction]: ...


@dataclass
class InMemoryProjectionStore:
    fail_commit: bool = False
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    agent_instances: dict[tuple[str, str | None, str], dict[str, Any]] = field(default_factory=dict)
    model_steps: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    tool_calls: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    tool_attempts: dict[tuple[str, str, int], dict[str, Any]] = field(default_factory=dict)
    policy_decisions: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def transaction(self) -> "InMemoryTransaction":
        return InMemoryTransaction(self)


class InMemoryTransaction:
    def __init__(self, store: InMemoryProjectionStore):
        self.store = store

    async def __aenter__(self) -> "InMemoryTransaction":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None and self.store.fail_commit:
            raise RuntimeError("commit failed")

    async def insert_event(self, event: Event) -> bool:
        if event.event_id in self.store.events:
            return False
        self.store.events[event.event_id] = _event_row(event)
        return True

    async def project_event(self, event: Event) -> None:
        project_event_into_maps(self.store, event)


class AsyncPostgresProjectionStore:
    def __init__(self, pool: Any):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str | None = None) -> "AsyncPostgresProjectionStore":
        import asyncpg

        pool = await asyncpg.create_pool(dsn or os.environ["DATABASE_URL"])
        return cls(pool)

    def transaction(self) -> "AsyncPostgresTransaction":
        return AsyncPostgresTransaction(self.pool)


class AsyncPostgresTransaction:
    def __init__(self, pool: Any):
        self.pool = pool
        self.conn: Any | None = None
        self.tx: Any | None = None

    async def __aenter__(self) -> "AsyncPostgresTransaction":
        self.conn = await self.pool.acquire()
        self.tx = self.conn.transaction()
        await self.tx.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        assert self.conn is not None and self.tx is not None
        try:
            if exc_type is None:
                await self.tx.commit()
            else:
                await self.tx.rollback()
        finally:
            await self.pool.release(self.conn)

    async def insert_event(self, event: Event) -> bool:
        assert self.conn is not None
        row = await self.conn.fetchrow(
            """
            INSERT INTO events (
              event_id, schema_version, run_id, agent_id, agent_instance_id, step_id,
              tool_call_id, attempt, sequence, kind, idempotency_key, traceparent,
              tracestate, trace_id, parent_id, timestamp, payload
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            event.event_id,
            event.schema_version,
            event.run_id,
            event.agent_id,
            event.agent_instance_id,
            event.step_id,
            event.tool_call_id,
            event.attempt,
            event.sequence,
            event.kind.value,
            event.idempotency_key,
            event.traceparent,
            event.tracestate,
            event.trace_id,
            event.parent_id,
            event.timestamp,
            json.dumps(event.payload, default=str),
        )
        return row is not None

    async def project_event(self, event: Event) -> None:
        assert self.conn is not None
        await _project_postgres(self.conn, event)


class Projector:
    def __init__(
        self,
        store: ProjectionStore,
        *,
        dead_letter_subject: str = "deadletter.audit.projector",
        dead_letter_publisher: Callable[[str, bytes], Awaitable[None]] | None = None,
    ):
        self.store = store
        self.dead_letter_subject = dead_letter_subject
        self.dead_letter_publisher = dead_letter_publisher

    async def project(self, event: Event) -> bool:
        async with self.store.transaction() as tx:
            inserted = await tx.insert_event(event)
            if inserted:
                await tx.project_event(event)
            return inserted

    async def handle_message(self, msg: Any) -> None:
        try:
            event = Event.model_validate_json(msg.data)
        except (ValueError, ValidationError) as exc:
            await self._dead_letter(msg, exc)
            await msg.ack()
            return
        try:
            await self.project(event)
        except Exception:
            if hasattr(msg, "nak"):
                await msg.nak()
            raise
        await msg.ack()

    async def _dead_letter(self, msg: Any, exc: Exception) -> None:
        payload = {
            "reason": "validation_error",
            "error": str(exc),
            "subject": getattr(msg, "subject", None),
            "data": msg.data.decode("utf-8", errors="replace"),
        }
        data = json.dumps(payload, sort_keys=True).encode()
        if self.dead_letter_publisher is not None:
            await self.dead_letter_publisher(self.dead_letter_subject, data)
        elif hasattr(msg, "publish"):
            await msg.publish(self.dead_letter_subject, data)


async def consume_projector_batch(js: Any, projector: Projector, *, batch: int = 50, timeout: float = 1.0) -> int:
    return await DurablePullProjector(js, projector, batch=batch, timeout=timeout).consume_batch()


class DurablePullProjector:
    def __init__(self, js: Any, projector: Projector, *, batch: int = 50, timeout: float = 1.0):
        self.js = js
        self.projector = projector
        self.batch = batch
        self.timeout = timeout
        self._subscription: Any | None = None

    async def consume_batch(self) -> int:
        subscription = await self._pull_subscription()
        try:
            messages = await subscription.fetch(self.batch, timeout=self.timeout)
        except (asyncio.TimeoutError, NatsTimeoutError, FetchTimeoutError):
            return 0
        for msg in messages:
            await self.projector.handle_message(msg)
        return len(messages)

    async def _pull_subscription(self) -> Any:
        if self._subscription is None:
            self._subscription = await self.js.pull_subscribe(
                "audit.>",
                durable=DURABLE_PROJECTOR_CONSUMER,
                stream="SWARMGUARD_AUDIT",
            )
        return self._subscription


def project_event_into_maps(store: InMemoryProjectionStore, event: Event) -> None:
    now = event.timestamp
    _upsert_state(
        store.runs,
        event.run_id,
        _run_state(event),
        event,
        extra={"run_id": event.run_id},
    )
    if event.agent_id and event.agent_instance_id:
        key = (event.run_id, event.agent_id, event.agent_instance_id)
        _upsert_state(store.agent_instances, key, _agent_state(event), event, extra={"agent_id": event.agent_id})
    if event.agent_instance_id and event.step_id:
        key = (event.run_id, event.agent_instance_id, event.step_id)
        _upsert_state(store.model_steps, key, _model_step_state(event), event, extra={"agent_id": event.agent_id})
    if event.tool_call_id:
        state = _event_state(event)
        key = (event.run_id, event.tool_call_id)
        _upsert_state(
            store.tool_calls,
            key,
            state,
            event,
            extra={"agent_id": event.agent_id, "tool": event.payload.get("tool")},
        )
        attempt_key = (event.run_id, event.tool_call_id, event.attempt)
        _upsert_state(
            store.tool_attempts,
            attempt_key,
            state,
            event,
            extra={"agent_id": event.agent_id, "tool": event.payload.get("tool"), "attempt": event.attempt},
        )
    if event.kind == EventKind.TOOL_POLICY_DECIDED and event.tool_call_id:
        decision_key = (event.run_id, event.tool_call_id, event.event_id)
        store.policy_decisions[decision_key] = {
            "run_id": event.run_id,
            "tool_call_id": event.tool_call_id,
            "decision": event.payload.get("decision"),
            "reason": event.payload.get("reason"),
            "payload": event.payload,
            "created_at": now,
        }
    artifact = event.payload.get("artifact")
    if isinstance(artifact, dict) and artifact.get("artifact_id"):
        artifact_id = str(artifact["artifact_id"])
        store.artifacts[artifact_id] = {
            "artifact_id": artifact_id,
            "run_id": event.run_id,
            "agent_id": event.agent_id,
            "agent_instance_id": event.agent_instance_id,
            "step_id": event.step_id,
            "tool_call_id": event.tool_call_id,
            "kind": artifact.get("kind"),
            "uri": artifact.get("uri"),
            "digest": artifact.get("digest"),
            "payload": artifact,
            "created_at": event.timestamp,
        }


def _upsert_state(
    rows: dict[Any, dict[str, Any]],
    key: Any,
    state: str,
    event: Event,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    current = rows.get(key)
    payload = {
        "state": state,
        "payload": event.payload,
        "first_event_id": event.event_id,
        "last_event_id": event.event_id,
        "created_at": event.timestamp,
        "updated_at": event.timestamp,
        "sequence": event.sequence,
    }
    if extra:
        payload.update(extra)
    if current is None:
        rows[key] = payload
        return
    if _state_rank(current["state"]) > _state_rank(state):
        return
    if current["state"] in TERMINAL_STATES and state not in TERMINAL_STATES:
        return
    current.update(payload)
    current["created_at"] = min(current["created_at"], event.timestamp)


def _event_state(event: Event) -> str:
    state = event.payload.get("state")
    if isinstance(state, str):
        return state
    return {
        EventKind.TOOL_REQUESTED: "requested",
        EventKind.TOOL_POLICY_DECIDED: "policy_decided",
        EventKind.TOOL_ALLOWED: "allowed",
        EventKind.TOOL_DISPATCHED: "dispatched",
        EventKind.TOOL_STARTED: "started",
        EventKind.TOOL_HEARTBEAT: "heartbeat",
        EventKind.TOOL_COMPLETED: "completed",
        EventKind.TOOL_FAILED: "failed",
        EventKind.TOOL_DENIED: "denied",
        EventKind.TOOL_TIMED_OUT: "timed_out",
        EventKind.TOOL_CANCELLED: "cancelled",
        EventKind.TOOL_DEAD_LETTER: "dead_letter",
    }.get(event.kind, "active")


def _run_state(event: Event) -> str:
    return _event_state(event) if event.kind.value.startswith("run.") else "active"


def _agent_state(event: Event) -> str:
    return _event_state(event) if event.kind.value.startswith("agent_session.") else "active"


def _model_step_state(event: Event) -> str:
    return _event_state(event) if event.kind.value.startswith("model_step.") else "active"


def _state_rank(state: str) -> int:
    return STATE_RANK.get(state, 0)


def _event_row(event: Event) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "schema_version": event.schema_version,
        "run_id": event.run_id,
        "agent_id": event.agent_id,
        "agent_instance_id": event.agent_instance_id,
        "step_id": event.step_id,
        "tool_call_id": event.tool_call_id,
        "attempt": event.attempt,
        "sequence": event.sequence,
        "kind": event.kind.value,
        "idempotency_key": event.idempotency_key,
        "traceparent": event.traceparent,
        "tracestate": event.tracestate,
        "trace_id": event.trace_id,
        "parent_id": event.parent_id,
        "timestamp": event.timestamp,
        "payload": event.payload,
    }


async def _project_postgres(conn: Any, event: Event) -> None:
    await conn.execute(
        """
        INSERT INTO runs (run_id, state, first_event_id, last_event_id, created_at, updated_at, payload)
        VALUES ($1, $2, $3, $3, $4, $4, $5::jsonb)
        ON CONFLICT (run_id) DO UPDATE SET
          state = CASE
            WHEN swarmguard_state_rank(runs.state) > swarmguard_state_rank(EXCLUDED.state)
            THEN runs.state ELSE EXCLUDED.state END,
          last_event_id = EXCLUDED.last_event_id,
          updated_at = EXCLUDED.updated_at,
          payload = EXCLUDED.payload
        """,
        event.run_id,
        _run_state(event),
        event.event_id,
        event.timestamp,
        json.dumps(event.payload, default=str),
    )
    if event.agent_id and event.agent_instance_id:
        await conn.execute(
            """
            INSERT INTO agent_instances (run_id, agent_id, agent_instance_id, state, first_event_id,
              last_event_id, created_at, updated_at, payload)
            VALUES ($1,$2,$3,$4,$5,$5,$6,$6,$7::jsonb)
            ON CONFLICT (run_id, agent_id, agent_instance_id) DO UPDATE SET
              state = CASE
                WHEN swarmguard_state_rank(agent_instances.state) > swarmguard_state_rank(EXCLUDED.state)
                THEN agent_instances.state ELSE EXCLUDED.state END,
              last_event_id = EXCLUDED.last_event_id,
              updated_at = EXCLUDED.updated_at,
              payload = EXCLUDED.payload
            """,
            event.run_id,
            event.agent_id,
            event.agent_instance_id,
            _agent_state(event),
            event.event_id,
            event.timestamp,
            json.dumps(event.payload, default=str),
        )
    if event.agent_instance_id and event.step_id:
        await conn.execute(
            """
            INSERT INTO model_steps (run_id, agent_id, agent_instance_id, step_id, state, first_event_id,
              last_event_id, created_at, updated_at, payload)
            VALUES ($1,$2,$3,$4,$5,$6,$6,$7,$7,$8::jsonb)
            ON CONFLICT (run_id, agent_instance_id, step_id) DO UPDATE SET
              state = CASE
                WHEN swarmguard_state_rank(model_steps.state) > swarmguard_state_rank(EXCLUDED.state)
                THEN model_steps.state ELSE EXCLUDED.state END,
              last_event_id = EXCLUDED.last_event_id,
              updated_at = EXCLUDED.updated_at,
              payload = EXCLUDED.payload
            """,
            event.run_id,
            event.agent_id,
            event.agent_instance_id,
            event.step_id,
            _model_step_state(event),
            event.event_id,
            event.timestamp,
            json.dumps(event.payload, default=str),
        )
    if event.tool_call_id:
        state = _event_state(event)
        await conn.execute(
            """
            INSERT INTO tool_calls (run_id, tool_call_id, agent_id, agent_instance_id, step_id, tool, state,
              first_event_id, last_event_id, created_at, updated_at, payload)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$8,$9,$9,$10::jsonb)
            ON CONFLICT (run_id, tool_call_id) DO UPDATE SET
              state = CASE
                WHEN swarmguard_state_rank(tool_calls.state) > swarmguard_state_rank(EXCLUDED.state)
                THEN tool_calls.state ELSE EXCLUDED.state END,
              last_event_id = EXCLUDED.last_event_id,
              updated_at = EXCLUDED.updated_at,
              payload = EXCLUDED.payload
            """,
            event.run_id,
            event.tool_call_id,
            event.agent_id,
            event.agent_instance_id,
            event.step_id,
            event.payload.get("tool"),
            state,
            event.event_id,
            event.timestamp,
            json.dumps(event.payload, default=str),
        )
        await conn.execute(
            """
            INSERT INTO tool_attempts (run_id, tool_call_id, attempt, state, first_event_id, last_event_id,
              created_at, updated_at, payload)
            VALUES ($1,$2,$3,$4,$5,$5,$6,$6,$7::jsonb)
            ON CONFLICT (run_id, tool_call_id, attempt) DO UPDATE SET
              state = CASE
                WHEN swarmguard_state_rank(tool_attempts.state) > swarmguard_state_rank(EXCLUDED.state)
                THEN tool_attempts.state ELSE EXCLUDED.state END,
              last_event_id = EXCLUDED.last_event_id,
              updated_at = EXCLUDED.updated_at,
              payload = EXCLUDED.payload
            """,
            event.run_id,
            event.tool_call_id,
            event.attempt,
            state,
            event.event_id,
            event.timestamp,
            json.dumps(event.payload, default=str),
        )
    if event.kind == EventKind.TOOL_POLICY_DECIDED and event.tool_call_id:
        await conn.execute(
            """
            INSERT INTO policy_decisions (run_id, tool_call_id, event_id, decision, reason, created_at, payload)
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
            ON CONFLICT (event_id) DO NOTHING
            """,
            event.run_id,
            event.tool_call_id,
            event.event_id,
            event.payload.get("decision"),
            event.payload.get("reason"),
            event.timestamp,
            json.dumps(event.payload, default=str),
        )
    artifact = event.payload.get("artifact")
    if isinstance(artifact, dict) and artifact.get("artifact_id"):
        await conn.execute(
            """
            INSERT INTO artifacts (artifact_id, run_id, agent_id, agent_instance_id, step_id, tool_call_id,
              kind, uri, digest, created_at, payload)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)
            ON CONFLICT (artifact_id) DO UPDATE SET
              uri = EXCLUDED.uri,
              digest = EXCLUDED.digest,
              payload = EXCLUDED.payload
            """,
            str(artifact["artifact_id"]),
            event.run_id,
            event.agent_id,
            event.agent_instance_id,
            event.step_id,
            event.tool_call_id,
            artifact.get("kind"),
            artifact.get("uri"),
            artifact.get("digest"),
            event.timestamp,
            json.dumps(artifact, default=str),
        )


async def run(database_url: str | None = None) -> None:
    store = await AsyncPostgresProjectionStore.connect(database_url)
    nc = await connect("projector")
    projector = Projector(store, dead_letter_publisher=nc.publish)
    try:
        js = nc.jetstream()
        runner = DurablePullProjector(js, projector)
        while True:
            await runner.consume_batch()
    finally:
        await nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()
    asyncio.run(run(args.database_url))


if __name__ == "__main__":
    main()
