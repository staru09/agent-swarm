from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.errors import FetchTimeoutError
import pytest

from swarmguard.projector import InMemoryProjectionStore, Projector
from swarmguard.protocol import Event, EventKind
from swarmguard.streams import DURABLE_PROJECTOR_CONSUMER


@dataclass
class FakeMessage:
    data: bytes
    subject: str = "audit.run-1.tool.requested"
    acked: bool = False
    nacked: bool = False
    published: list[tuple[str, bytes]] = field(default_factory=list)

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        self.nacked = True

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, data))


@dataclass
class MessageWithoutPublish:
    data: bytes
    subject: str = "audit.run-1.tool.requested"
    acked: bool = False

    async def ack(self) -> None:
        self.acked = True


class FakePullSubscription:
    def __init__(self, messages: list[FakeMessage]):
        self.messages = messages
        self.fetch_calls: list[tuple[int, float]] = []

    async def fetch(self, batch: int, timeout: float) -> list[FakeMessage]:
        self.fetch_calls.append((batch, timeout))
        messages, self.messages = self.messages, []
        return messages


class TimeoutThenMessageSubscription:
    def __init__(self, exception: BaseException, message: FakeMessage):
        self.exception = exception
        self.message = message
        self.fetch_calls = 0

    async def fetch(self, batch: int, timeout: float) -> list[FakeMessage]:
        self.fetch_calls += 1
        if self.fetch_calls == 1:
            raise self.exception
        return [self.message]


class FakePullJetStream:
    def __init__(self, messages: list[FakeMessage]):
        self.messages = messages
        self.pull_subscribe_calls: list[dict[str, object]] = []
        self.subscription = FakePullSubscription(messages)

    async def pull_subscribe(self, subject: str, durable: str, stream: str) -> FakePullSubscription:
        self.pull_subscribe_calls.append({"subject": subject, "durable": durable, "stream": stream})
        return self.subscription


def event(kind: EventKind, **overrides: object) -> Event:
    data = {
        "event_id": f"evt-{kind.value}-{overrides.get('sequence', 1)}",
        "run_id": "run-1",
        "agent_id": "operator",
        "agent_instance_id": "agent-instance-1",
        "step_id": "step-1",
        "tool_call_id": "tool-call-1",
        "attempt": 1,
        "sequence": 1,
        "kind": kind,
        "payload": {"tool": "safe_shell", "state": "requested", "request_id": "req-1"},
    }
    data.update(overrides)
    return Event(**data)


async def test_projector_inserts_append_only_event_once_and_projects_normalized_rows() -> None:
    store = InMemoryProjectionStore()
    projector = Projector(store)
    requested = event(EventKind.TOOL_REQUESTED)

    assert await projector.project(requested) is True
    assert await projector.project(requested) is False

    assert list(store.events) == [requested.event_id]
    assert store.runs["run-1"]["state"] == "active"
    assert store.agent_instances[("run-1", "operator", "agent-instance-1")]["state"] == "active"
    assert store.model_steps[("run-1", "agent-instance-1", "step-1")]["state"] == "active"
    assert store.tool_calls[("run-1", "tool-call-1")]["state"] == "requested"
    assert store.tool_attempts[("run-1", "tool-call-1", 1)]["state"] == "requested"


async def test_projector_out_of_order_lifecycle_events_do_not_regress_terminal_state() -> None:
    store = InMemoryProjectionStore()
    projector = Projector(store)

    await projector.project(
        event(
            EventKind.TOOL_COMPLETED,
            event_id="evt-completed",
            sequence=4,
            payload={"tool": "safe_shell", "state": "completed", "request_id": "req-1"},
        )
    )
    await projector.project(
        event(
            EventKind.TOOL_STARTED,
            event_id="evt-started-late",
            sequence=3,
            payload={"tool": "safe_shell", "state": "started", "request_id": "req-1"},
        )
    )

    assert store.tool_calls[("run-1", "tool-call-1")]["state"] == "completed"
    assert store.tool_attempts[("run-1", "tool-call-1", 1)]["state"] == "completed"
    assert store.runs["run-1"]["state"] == "active"


async def test_late_dead_letter_does_not_overwrite_existing_logical_terminal_state() -> None:
    store = InMemoryProjectionStore()
    projector = Projector(store)

    await projector.project(
        event(
            EventKind.TOOL_COMPLETED,
            event_id="evt-completed",
            sequence=4,
            payload={"tool": "safe_shell", "state": "completed", "request_id": "req-1"},
        )
    )
    await projector.project(
        event(
            EventKind.TOOL_DEAD_LETTER,
            event_id="evt-dead-letter-late",
            sequence=5,
            payload={"tool": "safe_shell", "state": "dead_letter", "request_id": "req-1"},
        )
    )

    assert store.tool_calls[("run-1", "tool-call-1")]["state"] == "completed"
    assert store.tool_attempts[("run-1", "tool-call-1", 1)]["state"] == "completed"
    assert set(store.events) == {"evt-completed", "evt-dead-letter-late"}


async def test_projector_lifecycle_events_terminalize_run_agent_and_model_without_regression() -> None:
    store = InMemoryProjectionStore()
    projector = Projector(store)

    await projector.project(
        event(
            EventKind.RUN_COMPLETED,
            event_id="run-completed",
            payload={"state": "completed", "status": "completed"},
        )
    )
    await projector.project(
        event(
            EventKind.AGENT_SESSION_COMPLETED,
            event_id="agent-completed",
            payload={"state": "completed", "status": "completed"},
        )
    )
    await projector.project(
        event(
            EventKind.MODEL_STEP_COMPLETED,
            event_id="step-completed",
            payload={"state": "completed", "status": "completed"},
        )
    )
    await projector.project(event(EventKind.RUN_STARTED, event_id="run-started-late", payload={"state": "active"}))
    await projector.project(
        event(EventKind.AGENT_SESSION_STARTED, event_id="agent-started-late", payload={"state": "active"})
    )
    await projector.project(event(EventKind.MODEL_STEP_STARTED, event_id="step-started-late", payload={"state": "active"}))

    assert store.runs["run-1"]["state"] == "completed"
    assert store.agent_instances[("run-1", "operator", "agent-instance-1")]["state"] == "completed"
    assert store.model_steps[("run-1", "agent-instance-1", "step-1")]["state"] == "completed"


async def test_projector_acknowledges_only_after_transaction_commit() -> None:
    store = InMemoryProjectionStore(fail_commit=True)
    projector = Projector(store)
    msg = FakeMessage(event(EventKind.TOOL_REQUESTED).model_dump_json().encode())

    with pytest.raises(RuntimeError, match="commit failed"):
        await projector.handle_message(msg)

    assert msg.acked is False
    assert msg.nacked is True

    store.fail_commit = False
    await projector.handle_message(msg)

    assert msg.acked is True


async def test_projector_dead_letters_malformed_events_then_acks() -> None:
    store = InMemoryProjectionStore()
    projector = Projector(store, dead_letter_subject="deadletter.audit.projector")
    msg = FakeMessage(b'{"event_id":"not-valid"}')

    await projector.handle_message(msg)

    assert msg.acked is True
    assert msg.nacked is False
    assert msg.published
    subject, payload = msg.published[0]
    assert subject == "deadletter.audit.projector"
    assert b"validation_error" in payload
    assert store.events == {}


async def test_projector_uses_injected_dead_letter_publisher_for_real_nats_messages() -> None:
    published: list[tuple[str, bytes]] = []

    async def publish(subject: str, data: bytes) -> None:
        published.append((subject, data))

    projector = Projector(InMemoryProjectionStore(), dead_letter_publisher=publish)
    msg = MessageWithoutPublish(b'{"event_id":"not-valid"}')

    await projector.handle_message(msg)

    assert msg.acked is True
    assert published[0][0] == "deadletter.audit.projector"


async def test_projector_fetches_from_bootstrapped_durable_pull_consumer() -> None:
    from swarmguard.projector import consume_projector_batch

    message = FakeMessage(event(EventKind.TOOL_REQUESTED).model_dump_json().encode())
    js = FakePullJetStream([message])
    store = InMemoryProjectionStore()

    handled = await consume_projector_batch(js, Projector(store), batch=10, timeout=0.5)

    assert handled == 1
    assert js.pull_subscribe_calls == [
        {"subject": "audit.>", "durable": DURABLE_PROJECTOR_CONSUMER, "stream": "SWARMGUARD_AUDIT"}
    ]
    assert js.subscription.fetch_calls == [(10, 0.5)]
    assert message.acked is True


async def test_projector_runner_reuses_precreated_pull_subscription_across_batches() -> None:
    from swarmguard.projector import DurablePullProjector

    first = FakeMessage(event(EventKind.TOOL_REQUESTED, event_id="evt-first").model_dump_json().encode())
    second = FakeMessage(event(EventKind.TOOL_STARTED, event_id="evt-second").model_dump_json().encode())
    js = FakePullJetStream([first])
    runner = DurablePullProjector(js, Projector(InMemoryProjectionStore()), batch=1, timeout=0.5)

    assert await runner.consume_batch() == 1
    js.subscription.messages = [second]
    assert await runner.consume_batch() == 1

    assert len(js.pull_subscribe_calls) == 1
    assert js.subscription.fetch_calls == [(1, 0.5), (1, 0.5)]


@pytest.mark.parametrize("exception", [asyncio.TimeoutError(), NatsTimeoutError(), FetchTimeoutError()])
async def test_projector_runner_treats_empty_fetch_timeouts_as_idle_and_reuses_subscription(exception: BaseException) -> None:
    from swarmguard.projector import DurablePullProjector

    message = FakeMessage(event(EventKind.TOOL_REQUESTED, event_id="evt-after-idle").model_dump_json().encode())
    js = FakePullJetStream([])
    js.subscription = TimeoutThenMessageSubscription(exception, message)  # type: ignore[assignment]
    runner = DurablePullProjector(js, Projector(InMemoryProjectionStore()), batch=1, timeout=0.01)

    assert await runner.consume_batch() == 0
    assert await runner.consume_batch() == 1

    assert len(js.pull_subscribe_calls) == 1
    assert js.subscription.fetch_calls == 2
    assert message.acked is True
