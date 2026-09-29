from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nats.js.api import ConsumerConfig

from swarmguard.streams import (
    DURABLE_PROJECTOR_CONSUMER,
    STREAM_TOPOLOGY_VERSION,
    bootstrap_jetstream,
    stream_topology,
    validate_topology,
)


@dataclass
class StoredStream:
    name: str
    config: dict[str, Any]


class FakeJetStream:
    def __init__(self) -> None:
        self.streams: dict[str, StoredStream] = {}
        self.consumers: dict[tuple[str, str], dict[str, Any]] = {}
        self.added_streams: list[dict[str, Any]] = []
        self.updated_streams: list[dict[str, Any]] = []

    async def stream_info(self, name: str) -> StoredStream:
        try:
            return self.streams[name]
        except KeyError as exc:
            raise LookupError(name) from exc

    async def add_stream(self, **config: Any) -> None:
        self.added_streams.append(config)
        self.streams[config["name"]] = StoredStream(config["name"], dict(config))

    async def update_stream(self, **config: Any) -> None:
        self.updated_streams.append(config)
        self.streams[config["name"]] = StoredStream(config["name"], dict(config))

    async def consumer_info(self, stream: str, durable_name: str) -> dict[str, Any]:
        try:
            return self.consumers[(stream, durable_name)]
        except KeyError as exc:
            raise LookupError((stream, durable_name)) from exc

    async def add_consumer(self, stream: str, config: Any) -> None:
        durable = config.durable_name if isinstance(config, ConsumerConfig) else config["durable_name"]
        self.consumers[(stream, durable)] = _consumer_dict(config)

    async def update_consumer(self, stream: str, durable_name: str, config: Any) -> None:
        self.consumers[(stream, durable_name)] = _consumer_dict(config)


def _consumer_dict(config: Any) -> dict[str, Any]:
    if isinstance(config, ConsumerConfig):
        return {
            "durable_name": config.durable_name,
            "filter_subject": config.filter_subject,
            "deliver_subject": config.deliver_subject,
            "ack_policy": config.ack_policy,
            "deliver_policy": config.deliver_policy,
            "replay_policy": config.replay_policy,
            "max_deliver": config.max_deliver,
            "ack_wait": config.ack_wait,
        }
    return dict(config)


def test_topology_defines_versioned_audit_command_retry_dead_letter_and_projector() -> None:
    topology = stream_topology()

    assert topology.version == STREAM_TOPOLOGY_VERSION
    assert {stream.name for stream in topology.streams} == {
        "SWARMGUARD_AUDIT",
        "SWARMGUARD_COMMANDS",
        "SWARMGUARD_RETRY",
        "SWARMGUARD_DEAD_LETTER",
    }
    audit = topology.stream("SWARMGUARD_AUDIT")
    assert audit.subjects == ("audit.>",)
    assert audit.retention == "limits"
    assert audit.storage == "file"
    assert audit.max_age_seconds >= 7 * 24 * 60 * 60
    assert audit.max_bytes >= 1024 * 1024 * 1024
    assert audit.duplicate_window_seconds >= 120

    consumer = topology.consumer("SWARMGUARD_AUDIT", DURABLE_PROJECTOR_CONSUMER)
    assert consumer.filter_subject == "audit.>"
    assert consumer.mode == "pull"
    assert consumer.deliver_subject is None
    assert consumer.ack_policy == "explicit"
    assert consumer.deliver_policy == "all"
    assert consumer.replay_policy == "instant"
    assert consumer.max_deliver >= 3

    validate_topology(topology)


async def test_bootstrap_jetstream_adds_missing_streams_and_updates_drift_idempotently() -> None:
    js = FakeJetStream()

    await bootstrap_jetstream(js)
    first_added = len(js.added_streams)
    await bootstrap_jetstream(js)

    assert first_added == 4
    assert len(js.added_streams) == 4
    assert js.updated_streams == []

    js.streams["SWARMGUARD_AUDIT"].config["subjects"] = ["audit.old.>"]
    await bootstrap_jetstream(js)

    assert js.updated_streams[-1]["name"] == "SWARMGUARD_AUDIT"
    assert js.updated_streams[-1]["subjects"] == ["audit.>"]


async def test_bootstrap_jetstream_uses_nats_py_consumer_config_objects() -> None:
    class StrictJetStream(FakeJetStream):
        async def add_consumer(self, stream: str, config: ConsumerConfig) -> None:  # type: ignore[override]
            assert isinstance(config, ConsumerConfig)
            self.consumers[(stream, config.durable_name)] = {"durable_name": config.durable_name}

        async def update_consumer(self, stream: str, durable_name: str, config: ConsumerConfig) -> None:  # type: ignore[override]
            assert isinstance(config, ConsumerConfig)
            self.consumers[(stream, durable_name)] = {"durable_name": config.durable_name}

    await bootstrap_jetstream(StrictJetStream())


async def test_bootstrap_reconciles_consumer_drift_without_update_consumer_api() -> None:
    class NoUpdateConsumerJetStream(FakeJetStream):
        update_consumer = None

        def __init__(self) -> None:
            super().__init__()
            self.consumers[("SWARMGUARD_AUDIT", DURABLE_PROJECTOR_CONSUMER)] = {
                "durable_name": DURABLE_PROJECTOR_CONSUMER,
                "filter_subject": "audit.old.>",
            }

        async def add_consumer(self, stream: str, config: ConsumerConfig) -> None:  # type: ignore[override]
            assert isinstance(config, ConsumerConfig)
            self.consumers[(stream, config.durable_name)] = _consumer_dict(config)

    js = NoUpdateConsumerJetStream()

    await bootstrap_jetstream(js)

    assert js.consumers[("SWARMGUARD_AUDIT", DURABLE_PROJECTOR_CONSUMER)]["filter_subject"] == "audit.>"


def test_declared_streams_fit_the_nats_file_store_reservation() -> None:
    config = (Path(__file__).resolve().parents[1] / "infra" / "nats" / "nats.conf").read_text(encoding="utf-8")
    store_gib = int(re.search(r"max_file_store: (\d+)GB", config).group(1))

    assert sum(stream.max_bytes for stream in stream_topology().streams) <= store_gib * 1024**3
