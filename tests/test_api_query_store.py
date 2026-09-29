from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swarmguard.api import (
    AsyncPostgresQueryStore,
    InMemoryQueryStore,
    PageParams,
    QueryStoreUnavailable,
    Timeline,
    app,
    replay_after_cursor,
    require_admin,
    require_operator,
    require_viewer,
    timeline,
)
from swarmguard.security import TokenClaims
from swarmguard.projector import InMemoryProjectionStore, Projector
from swarmguard.protocol import Event, EventKind


def event(kind: EventKind, **overrides: object) -> Event:
    data = {
        "event_id": f"evt-{overrides.get('sequence', 1)}",
        "run_id": "run-1",
        "agent_id": "operator",
        "agent_instance_id": "agent-instance-1",
        "step_id": "step-1",
        "tool_call_id": "tool-call-1",
        "attempt": 1,
        "sequence": 1,
        "kind": kind,
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "parent_id": "00f067aa0ba902b7",
        "payload": {"tool": "safe_shell", "state": "requested", "request_id": "req-1"},
    }
    data.update(overrides)
    return Event(**data)


async def projected_store(*events: Event) -> InMemoryProjectionStore:
    store = InMemoryProjectionStore()
    projector = Projector(store)
    for item in events:
        await projector.project(item)
    return store


@pytest.fixture(autouse=True)
def isolated_query_store() -> None:
    original = app.dependency_overrides.copy()

    def _fake_viewer() -> TokenClaims:
        import time

        now = time.time()
        return TokenClaims(sub="test", role="viewer", iss="swarmguard", aud="swarmguard-api", iat=now, exp=now + 3600)

    # These query-store tests focus on pagination/projection semantics, so the
    # RBAC dependency is overridden to a fixed viewer. Dedicated auth coverage
    # lives in tests/test_api_auth.py.
    app.dependency_overrides[require_viewer] = _fake_viewer
    app.dependency_overrides[require_operator] = _fake_viewer
    app.dependency_overrides[require_admin] = _fake_viewer
    timeline.query_store = InMemoryQueryStore(InMemoryProjectionStore())
    yield
    timeline.query_store = None
    app.dependency_overrides = original


async def test_runs_are_paginated_by_durable_query_store_with_bounded_limits() -> None:
    store = await projected_store(
        event(EventKind.RUN_STARTED, event_id="run-a-start", run_id="run-a", sequence=1, payload={"state": "active"}),
        event(
            EventKind.RUN_COMPLETED,
            event_id="run-b-done",
            run_id="run-b",
            sequence=2,
            payload={"state": "completed"},
        ),
    )
    timeline.query_store = InMemoryQueryStore(store)

    with TestClient(app) as client:
        first = client.get("/api/runs", params={"limit": 1})
        assert first.status_code == 200
        assert first.json()["items"][0]["run_id"] == "run-a"
        cursor = first.json()["next_cursor"]

        second = client.get("/api/runs", params={"limit": 1, "cursor": cursor})
        assert second.status_code == 200
        assert second.json()["items"][0]["run_id"] == "run-b"
        assert second.json()["next_cursor"] is None

        too_large = client.get("/api/runs", params={"limit": 101})
        assert too_large.status_code == 422


async def test_run_detail_exposes_related_projection_rows_and_missing_ids_404() -> None:
    store = await projected_store(
        event(EventKind.RUN_STARTED, event_id="run-start", sequence=1, payload={"state": "active"}),
        event(EventKind.AGENT_SESSION_STARTED, event_id="agent-start", sequence=2, payload={"state": "active"}),
        event(EventKind.MODEL_STEP_STARTED, event_id="step-start", sequence=3, payload={"state": "active"}),
        event(EventKind.TOOL_REQUESTED, event_id="tool-request", sequence=4),
        event(
            EventKind.TOOL_POLICY_DECIDED,
            event_id="policy",
            sequence=5,
            payload={
                "tool": "safe_shell",
                "state": "policy_decided",
                "request_id": "req-1",
                "decision": "allowed",
                "reason": "policy-v1",
                "policy_version": "v1",
            },
        ),
        event(
            EventKind.TOOL_FAILED,
            event_id="attempt-1-failed",
            sequence=6,
            payload={"tool": "safe_shell", "state": "failed", "latency_ms": 25},
        ),
        event(
            EventKind.TOOL_COMPLETED,
            event_id="attempt-2-done",
            sequence=7,
            attempt=2,
            payload={
                "tool": "safe_shell",
                "state": "completed",
                "latency_ms": 30,
                "artifact": {
                    "artifact_id": "artifact-1",
                    "kind": "evidence",
                    "uri": "file:///tmp/evidence.json",
                    "digest": "sha256:abc",
                    "kernel_trace_id": "kernel-1",
                },
            },
        ),
    )
    timeline.query_store = InMemoryQueryStore(store)

    with TestClient(app) as client:
        detail = client.get("/api/runs/run-1")
        assert detail.status_code == 200
        body = detail.json()
        assert body["run"]["run_id"] == "run-1"
        assert body["agents"][0]["agent_instance_id"] == "agent-instance-1"
        assert body["model_steps"][0]["step_id"] == "step-1"
        assert body["tool_calls"][0]["state"] == "completed"
        assert body["tool_calls"][0]["latency_ms"] == 30
        assert body["tool_attempts"][0]["attempt"] == 1
        assert body["tool_attempts"][1]["attempt"] == 2
        assert body["policy_decisions"][0]["policy_version"] == "v1"
        assert body["artifacts"][0]["linked_kernel_evidence"] == "kernel-1"

        missing = client.get("/api/runs/missing")
        assert missing.status_code == 404


async def test_events_preserve_compatibility_and_validate_filters() -> None:
    store = await projected_store(
        event(EventKind.TOOL_REQUESTED, event_id="requested", sequence=1),
        event(EventKind.TOOL_COMPLETED, event_id="completed", sequence=2),
        event(EventKind.TOOL_REQUESTED, event_id="other-run", run_id="run-2", sequence=3),
    )
    timeline.query_store = InMemoryQueryStore(store)

    with TestClient(app) as client:
        by_run = client.get("/api/events", params={"run_id": "run-1"})
        assert by_run.status_code == 200
        assert [item["event_id"] for item in by_run.json()["items"]] == ["requested", "completed"]

        by_kind = client.get("/api/events", params={"kind": "tool.completed"})
        assert by_kind.status_code == 200
        assert [item["event_id"] for item in by_kind.json()["items"]] == ["completed"]

        invalid = client.get("/api/events", params={"kind": "not-a-kind"})
        assert invalid.status_code == 422


async def test_trace_detail_returns_events_artifacts_and_tool_attempts() -> None:
    store = await projected_store(
        event(EventKind.TOOL_REQUESTED, event_id="requested", sequence=1),
        event(
            EventKind.KERNEL_ALERT,
            event_id="kernel",
            sequence=2,
            tool_call_id=None,
            payload={"artifact": {"artifact_id": "kernel-artifact", "kind": "kernel", "kernel_trace_id": "kernel-1"}},
        ),
    )
    timeline.query_store = InMemoryQueryStore(store)

    with TestClient(app) as client:
        response = client.get("/api/traces/4bf92f3577b34da6a3ce929d0e0e4736")
        assert response.status_code == 200
        body = response.json()
        assert [item["event_id"] for item in body["events"]] == ["requested", "kernel"]
        assert body["tool_attempts"][0]["tool_call_id"] == "tool-call-1"
        assert body["artifacts"][0]["artifact_id"] == "kernel-artifact"


async def test_live_catch_up_orders_database_events_before_live_and_deduplicates() -> None:
    store = await projected_store(
        event(EventKind.TOOL_REQUESTED, event_id="old", sequence=1),
        event(EventKind.TOOL_STARTED, event_id="catch-up", sequence=2),
        event(EventKind.TOOL_COMPLETED, event_id="also-live", sequence=3),
    )
    query_store = InMemoryQueryStore(store)

    merged = await replay_after_cursor(
        query_store,
        last_cursor="1:old",
        live_events=[
            event(EventKind.TOOL_COMPLETED, event_id="also-live", sequence=3),
            event(EventKind.KERNEL_ALERT, event_id="live", sequence=4, tool_call_id=None),
        ],
    )

    assert [item.event_id for item in merged] == ["catch-up", "also-live", "live"]


async def test_query_store_failures_return_service_unavailable() -> None:
    class BrokenStore(InMemoryQueryStore):
        async def list_runs(self, page):  # type: ignore[no-untyped-def]
            raise QueryStoreUnavailable("database unavailable")

    timeline.query_store = BrokenStore(InMemoryProjectionStore())

    with TestClient(app) as client:
        response = client.get("/api/runs")

    assert response.status_code == 503
    assert response.json()["detail"] == "database unavailable"


async def test_live_registration_buffers_event_arriving_during_replay_once_in_order() -> None:
    during = event(EventKind.TOOL_COMPLETED, event_id="during-replay", sequence=3)
    store = await projected_store(
        event(EventKind.TOOL_REQUESTED, event_id="old", sequence=1),
        event(EventKind.TOOL_STARTED, event_id="catch-up", sequence=2),
    )
    timeline_for_test = Timeline()

    class InjectingStore(InMemoryQueryStore):
        async def list_events(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            await timeline_for_test._receive(FakeLiveMessage(during))
            return await super().list_events(*args, **kwargs)

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_text(self, payload: str) -> None:
            self.sent.append(Event.model_validate_json(payload).event_id)

    timeline_for_test.query_store = InjectingStore(store)
    websocket = FakeWebSocket()

    await timeline_for_test.register_with_catch_up(websocket, "1:old")

    assert websocket.sent == ["catch-up", "during-replay"]


async def test_live_registration_drains_all_database_pages_before_buffered_live_events() -> None:
    live = event(EventKind.KERNEL_ALERT, event_id="live-during-page-fetch", sequence=152, tool_call_id=None)
    timeline_for_test = Timeline()

    class PagedStore(InMemoryQueryStore):
        def __init__(self) -> None:
            self.calls: list[str | None] = []
            super().__init__(InMemoryProjectionStore())

        async def list_events(self, page: PageParams, *, after_cursor=None, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(after_cursor)
            if len(self.calls) == 1:
                await timeline_for_test._receive(FakeLiveMessage(live))
                items = [
                    event(EventKind.TOOL_STARTED, event_id=f"catch-up-{sequence:03d}", sequence=sequence).model_dump(mode="json")
                    for sequence in range(2, 102)
                ]
                return type("PageObject", (), {"items": items, "next_cursor": "101:catch-up-101"})()
            if len(self.calls) == 2:
                items = [
                    event(EventKind.TOOL_HEARTBEAT, event_id=f"catch-up-{sequence:03d}", sequence=sequence).model_dump(mode="json")
                    for sequence in range(102, 152)
                ]
                return type("PageObject", (), {"items": items, "next_cursor": None})()
            raise AssertionError("unexpected extra page fetch")

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_text(self, payload: str) -> None:
            self.sent.append(Event.model_validate_json(payload).event_id)

    store = PagedStore()
    timeline_for_test.query_store = store
    websocket = FakeWebSocket()

    await timeline_for_test.register_with_catch_up(websocket, "1:old")

    assert len(websocket.sent) == 151
    assert websocket.sent[:3] == ["catch-up-002", "catch-up-003", "catch-up-004"]
    assert websocket.sent[99:102] == ["catch-up-101", "catch-up-102", "catch-up-103"]
    assert websocket.sent[-1] == "live-during-page-fetch"
    assert websocket.sent.count("live-during-page-fetch") == 1
    assert store.calls == ["1:old", "101:catch-up-101"]


class FakeLiveMessage:
    def __init__(self, item: Event):
        self.data = item.model_dump_json().encode()
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


async def test_state_filters_are_validated_for_known_projection_states() -> None:
    store = await projected_store(event(EventKind.TOOL_REQUESTED, event_id="requested", sequence=1))
    timeline.query_store = InMemoryQueryStore(store)

    with TestClient(app) as client:
        valid = client.get("/api/runs/run-1/tool-calls", params={"state": "requested"})
        assert valid.status_code == 200
        assert [item["event_id"] for item in valid.json()["items"]] == ["requested"]

        invalid = client.get("/api/runs/run-1/tool-calls", params={"state": "not-a-state"})
        assert invalid.status_code == 422


async def test_in_memory_and_postgres_run_pagination_use_run_id_order_and_cursor() -> None:
    memory = await projected_store(
        event(EventKind.RUN_STARTED, event_id="run-b-start", run_id="run-b", sequence=1, payload={"state": "active"}),
        event(EventKind.RUN_STARTED, event_id="run-a-start", run_id="run-a", sequence=2, payload={"state": "active"}),
    )
    memory_page = await InMemoryQueryStore(memory).list_runs(PageParams(limit=1))
    memory_next = await InMemoryQueryStore(memory).list_runs(PageParams(limit=1, cursor=memory_page.next_cursor))

    postgres_pool = FakeAsyncPgPool(
        [
            {
                "run_id": "run-b",
                "state": "active",
                "payload": "{}",
            }
        ]
    )
    postgres_page = await AsyncPostgresQueryStore(postgres_pool).list_runs(PageParams(limit=1, cursor="run-a"))

    assert [item["run_id"] for item in memory_page.items] == ["run-a"]
    assert memory_page.next_cursor == "run-a"
    assert [item["run_id"] for item in memory_next.items] == ["run-b"]
    assert [item["run_id"] for item in postgres_page.items] == ["run-b"]
    assert postgres_pool.calls[0][1] == ("run-a", 2)


async def test_event_responses_include_normalized_projection_fields_for_memory_store() -> None:
    store = await projected_store(
        event(
            EventKind.TOOL_COMPLETED,
            event_id="completed",
            sequence=1,
            payload={
                "tool": "safe_shell",
                "state": "completed",
                "latency_ms": 42,
                "policy_version": "v2",
                "kernel_trace_id": "kernel-2",
            },
        )
    )
    timeline.query_store = InMemoryQueryStore(store)

    with TestClient(app) as client:
        response = client.get("/api/events")

    item = response.json()["items"][0]
    assert item["latency_ms"] == 42
    assert item["policy_version"] == "v2"
    assert item["linked_kernel_evidence"] == "kernel-2"


def test_health_exposes_when_durable_database_backing_is_absent() -> None:
    timeline.query_store = InMemoryQueryStore(InMemoryProjectionStore())

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["database"] == {"durable": False, "backend": "memory"}


class FakeAsyncPgPool:
    def __init__(self, rows: list[dict[str, object]] | None = None, *, fail: bool = False):
        self.rows = rows or []
        self.fail = fail
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, *args: object):
        self.calls.append((sql, args))
        if self.fail:
            raise RuntimeError("pool down")
        return self.rows


async def test_postgres_events_use_keyset_params_and_decode_json_projection_fields() -> None:
    pool = FakeAsyncPgPool(
        [
            {
                "event_id": "event-2",
                "run_id": "run-1",
                "sequence": 2,
                "kind": "tool.completed",
                "payload": '{"latency_ms": 44, "policy_version": "v3", "kernel_trace_id": "kernel-3"}',
            }
        ]
    )
    store = AsyncPostgresQueryStore(pool)

    page = await store.list_events(PageParams(limit=10), run_id="run-1", kind=EventKind.TOOL_COMPLETED, after_cursor="1:event-1")

    assert page.items[0]["payload"] == {"latency_ms": 44, "policy_version": "v3", "kernel_trace_id": "kernel-3"}
    assert page.items[0]["latency_ms"] == 44
    assert page.items[0]["policy_version"] == "v3"
    assert page.items[0]["linked_kernel_evidence"] == "kernel-3"
    assert pool.calls[0][1] == ("run-1", "tool.completed", 1, "event-1", 11)


async def test_postgres_missing_trace_returns_none_and_pool_errors_map_to_query_store_unavailable() -> None:
    missing = await AsyncPostgresQueryStore(FakeAsyncPgPool([])).get_trace("trace-missing")
    assert missing is None

    with pytest.raises(QueryStoreUnavailable):
        await AsyncPostgresQueryStore(FakeAsyncPgPool(fail=True)).list_runs(PageParams(limit=1))
