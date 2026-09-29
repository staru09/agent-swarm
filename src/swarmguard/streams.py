from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from nats.js.api import ConsumerConfig

from .bus import connect


STREAM_TOPOLOGY_VERSION = 1
DURABLE_PROJECTOR_CONSUMER = "swarmguard-projector-v1"


@dataclass(frozen=True)
class StreamSpec:
    name: str
    subjects: tuple[str, ...]
    retention: str
    storage: str
    max_age_seconds: int
    max_bytes: int
    duplicate_window_seconds: int

    def as_nats_config(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "subjects": list(self.subjects),
            "retention": self.retention,
            "storage": self.storage,
            "max_age": self.max_age_seconds,
            "max_bytes": self.max_bytes,
            "duplicate_window": self.duplicate_window_seconds,
        }


@dataclass(frozen=True)
class ConsumerSpec:
    stream: str
    durable_name: str
    filter_subject: str
    mode: str
    deliver_subject: str | None
    ack_policy: str
    deliver_policy: str
    replay_policy: str
    max_deliver: int
    ack_wait_seconds: int

    def as_nats_config(self) -> dict[str, Any]:
        return {
            "durable_name": self.durable_name,
            "filter_subject": self.filter_subject,
            "deliver_subject": self.deliver_subject,
            "ack_policy": self.ack_policy,
            "deliver_policy": self.deliver_policy,
            "replay_policy": self.replay_policy,
            "max_deliver": self.max_deliver,
            "ack_wait": self.ack_wait_seconds,
        }


@dataclass(frozen=True)
class StreamTopology:
    version: int
    streams: tuple[StreamSpec, ...]
    consumers: tuple[ConsumerSpec, ...]

    def stream(self, name: str) -> StreamSpec:
        for stream in self.streams:
            if stream.name == name:
                return stream
        raise KeyError(name)

    def consumer(self, stream: str, durable_name: str) -> ConsumerSpec:
        for consumer in self.consumers:
            if consumer.stream == stream and consumer.durable_name == durable_name:
                return consumer
        raise KeyError((stream, durable_name))


class JetStreamManager(Protocol):
    async def stream_info(self, name: str) -> Any: ...
    async def add_stream(self, **config: Any) -> Any: ...
    async def update_stream(self, **config: Any) -> Any: ...
    async def consumer_info(self, stream: str, durable_name: str) -> Any: ...
    async def add_consumer(self, stream: str, config: ConsumerConfig) -> Any: ...


def stream_topology() -> StreamTopology:
    gib = 1024 * 1024 * 1024
    # JetStream reserves max_bytes up front: the sum must fit nats.conf max_file_store (5GB).
    return StreamTopology(
        version=STREAM_TOPOLOGY_VERSION,
        streams=(
            StreamSpec("SWARMGUARD_AUDIT", ("audit.>",), "limits", "file", 30 * 24 * 60 * 60, 3 * gib, 300),
            StreamSpec("SWARMGUARD_COMMANDS", ("commands.>",), "workqueue", "file", 7 * 24 * 60 * 60, gib // 2, 300),
            StreamSpec("SWARMGUARD_RETRY", ("retry.>",), "limits", "file", 7 * 24 * 60 * 60, gib // 2, 300),
            StreamSpec("SWARMGUARD_DEAD_LETTER", ("deadletter.>",), "limits", "file", 90 * 24 * 60 * 60, gib // 2, 300),
        ),
        consumers=(
            ConsumerSpec(
                stream="SWARMGUARD_AUDIT",
                durable_name=DURABLE_PROJECTOR_CONSUMER,
                filter_subject="audit.>",
                mode="pull",
                deliver_subject=None,
                ack_policy="explicit",
                deliver_policy="all",
                replay_policy="instant",
                max_deliver=5,
                ack_wait_seconds=30,
            ),
        ),
    )


def validate_topology(topology: StreamTopology) -> None:
    names = [stream.name for stream in topology.streams]
    if len(names) != len(set(names)):
        raise ValueError("stream names must be unique")
    stream_names = set(names)
    for stream in topology.streams:
        if not stream.subjects:
            raise ValueError(f"{stream.name} must define subjects")
        if stream.retention not in {"limits", "workqueue"}:
            raise ValueError(f"{stream.name} has unsupported retention")
        if stream.storage not in {"file", "memory"}:
            raise ValueError(f"{stream.name} has unsupported storage")
        if stream.max_age_seconds <= 0 or stream.max_bytes <= 0 or stream.duplicate_window_seconds <= 0:
            raise ValueError(f"{stream.name} limits must be positive")
    for consumer in topology.consumers:
        if consumer.stream not in stream_names:
            raise ValueError(f"consumer references unknown stream: {consumer.stream}")
        if consumer.ack_policy != "explicit":
            raise ValueError("projector consumer must use explicit ack")
        if consumer.mode not in {"pull", "push"}:
            raise ValueError("consumer mode must be pull or push")
        if consumer.mode == "pull" and consumer.deliver_subject is not None:
            raise ValueError("pull consumers must not define deliver_subject")
        if consumer.mode == "push" and not consumer.deliver_subject:
            raise ValueError("push consumers must define deliver_subject")
        if consumer.max_deliver <= 0 or consumer.ack_wait_seconds <= 0:
            raise ValueError("consumer retry settings must be positive")


async def bootstrap_jetstream(js: JetStreamManager, topology: StreamTopology | None = None) -> None:
    topology = topology or stream_topology()
    validate_topology(topology)
    for stream in topology.streams:
        desired = stream.as_nats_config()
        try:
            current = await js.stream_info(stream.name)
        except Exception:
            await js.add_stream(**desired)
        else:
            if _stream_drifted(current, desired):
                await js.update_stream(**desired)
    for consumer in topology.consumers:
        desired = consumer.as_nats_config()
        desired_config = _consumer_config(consumer)
        try:
            current = await js.consumer_info(consumer.stream, consumer.durable_name)
        except Exception:
            await js.add_consumer(consumer.stream, desired_config)
        else:
            if _consumer_drifted(current, desired):
                await js.add_consumer(consumer.stream, desired_config)


def _stream_drifted(current: Any, desired: dict[str, Any]) -> bool:
    config = _config_dict(current)
    for key, value in desired.items():
        current_value = config.get(key)
        if key == "subjects" and tuple(current_value or ()) == tuple(value):
            continue
        if current_value != value:
            return True
    return False


def _consumer_drifted(current: Any, desired: dict[str, Any]) -> bool:
    config = _config_dict(current)
    return any(config.get(key) != value for key, value in desired.items())


def _consumer_config(consumer: ConsumerSpec) -> ConsumerConfig:
    return ConsumerConfig(
        durable_name=consumer.durable_name,
        filter_subject=consumer.filter_subject,
        deliver_subject=consumer.deliver_subject,
        ack_policy=consumer.ack_policy,
        deliver_policy=consumer.deliver_policy,
        replay_policy=consumer.replay_policy,
        max_deliver=consumer.max_deliver,
        ack_wait=consumer.ack_wait_seconds,
    )


def _config_dict(value: Any) -> dict[str, Any]:
    config = getattr(value, "config", value)
    if isinstance(config, dict):
        return dict(config)
    return {key: getattr(config, key) for key in dir(config) if not key.startswith("_")}


async def run_bootstrap() -> None:
    nc = await connect("stream-bootstrap")
    try:
        await bootstrap_jetstream(nc.jetstream())
    finally:
        await nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    asyncio.run(run_bootstrap())


if __name__ == "__main__":
    main()
