from __future__ import annotations

from typing import Any

import pytest

from swarmguard.projector import AsyncPostgresProjectionStore, Projector
from swarmguard.protocol import Event, EventKind


class FakeAsyncpgTransaction:
    def __init__(self) -> None:
        self.started = 0
        self.committed = 0
        self.rolled_back = 0

    async def start(self) -> None:
        self.started += 1

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        self.rolled_back += 1


class FakeAsyncpgConnection:
    def __init__(self, *, insert_row: dict[str, str] | None = {"event_id": "evt-1"}, fail_on: str | None = None):
        self.insert_row = insert_row
        self.fail_on = fail_on
        self.tx = FakeAsyncpgTransaction()
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeAsyncpgTransaction:
        return self.tx

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, str] | None:
        self.fetchrow_calls.append((sql, args))
        return self.insert_row

    async def execute(self, sql: str, *args: Any) -> None:
        self.execute_calls.append((sql, args))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("projection failed")


class FakeAsyncpgPool:
    def __init__(self, conn: FakeAsyncpgConnection):
        self.conn = conn
        self.acquired = 0
        self.released = 0

    async def acquire(self) -> FakeAsyncpgConnection:
        self.acquired += 1
        return self.conn

    async def release(self, conn: FakeAsyncpgConnection) -> None:
        assert conn is self.conn
        self.released += 1


def projection_event(kind: EventKind = EventKind.TOOL_POLICY_DECIDED, **overrides: object) -> Event:
    data = {
        "event_id": "evt-1",
        "run_id": "run-1",
        "agent_id": "operator",
        "agent_instance_id": "agent-instance-1",
        "step_id": "step-1",
        "tool_call_id": "tool-call-1",
        "attempt": 2,
        "sequence": 4,
        "kind": kind,
        "payload": {
            "tool": "safe_shell",
            "state": "policy_decided",
            "request_id": "req-1",
            "decision": "allowed",
            "reason": "tool allowed",
            "artifact": {
                "artifact_id": "artifact-1",
                "kind": "tool-output",
                "uri": "s3://bucket/key",
                "digest": "sha256:abc",
            },
        },
    }
    data.update(overrides)
    return Event(**data)


async def test_asyncpg_store_commits_insert_and_projection_writes_all_tables() -> None:
    conn = FakeAsyncpgConnection()
    pool = FakeAsyncpgPool(conn)

    inserted = await Projector(AsyncPostgresProjectionStore(pool)).project(projection_event())

    assert inserted is True
    assert conn.tx.started == 1
    assert conn.tx.committed == 1
    assert conn.tx.rolled_back == 0
    assert pool.released == 1
    sql = "\n".join(call[0] for call in conn.execute_calls)
    insert_sql, insert_args = conn.fetchrow_calls[0]
    assert "ON CONFLICT (event_id) DO NOTHING" in insert_sql
    assert insert_args[0] == "evt-1"
    assert insert_args[2] == "run-1"
    assert insert_args[3] == "operator"
    assert insert_args[6] == "tool-call-1"
    assert insert_args[7] == 2
    assert insert_args[9] == EventKind.TOOL_POLICY_DECIDED.value
    for table in [
        "runs",
        "agent_instances",
        "model_steps",
        "tool_calls",
        "tool_attempts",
        "policy_decisions",
        "artifacts",
    ]:
        assert f"INSERT INTO {table}" in sql


async def test_asyncpg_store_skips_projection_for_duplicate_event_but_still_commits() -> None:
    conn = FakeAsyncpgConnection(insert_row=None)

    inserted = await Projector(AsyncPostgresProjectionStore(FakeAsyncpgPool(conn))).project(projection_event())

    assert inserted is False
    assert conn.execute_calls == []
    assert conn.tx.committed == 1
    assert conn.tx.rolled_back == 0


async def test_asyncpg_store_rolls_back_and_releases_connection_on_projection_error() -> None:
    conn = FakeAsyncpgConnection(fail_on="INSERT INTO tool_calls")

    with pytest.raises(RuntimeError, match="projection failed"):
        await Projector(AsyncPostgresProjectionStore(FakeAsyncpgPool(conn))).project(projection_event())

    assert conn.tx.committed == 0
    assert conn.tx.rolled_back == 1


async def test_asyncpg_projection_sql_uses_rank_guards_for_out_of_order_updates() -> None:
    conn = FakeAsyncpgConnection()

    await Projector(AsyncPostgresProjectionStore(FakeAsyncpgPool(conn))).project(projection_event())

    sql = "\n".join(call[0] for call in conn.execute_calls)
    assert "swarmguard_state_rank" in sql
    assert "tool_calls.state" in sql
    assert "tool_attempts.state" in sql
    assert "agent_instances.state" in sql
    assert "model_steps.state" in sql
