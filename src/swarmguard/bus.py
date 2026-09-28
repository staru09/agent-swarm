from __future__ import annotations

import os

import nats
from nats.aio.client import Client as NATS

from .protocol import Event, event_subject


async def connect(name: str) -> NATS:
    options: dict[str, object] = {
        "servers": [os.getenv("NATS_URL", "nats://127.0.0.1:4222")],
        "name": name,
        "connect_timeout": 5,
        "max_reconnect_attempts": -1,
    }
    credentials = os.getenv("NATS_CREDS")
    if credentials:
        options["user_credentials"] = credentials
    else:
        if os.getenv("NATS_USER"):
            options["user"] = os.environ["NATS_USER"]
        if os.getenv("NATS_PASSWORD"):
            options["password"] = os.environ["NATS_PASSWORD"]
    return await nats.connect(**options)


async def publish_event(nc: NATS, event: Event) -> None:
    await nc.publish(
        event_subject(event.run_id, event.kind),
        event.model_dump_json().encode(),
        headers={"Nats-Msg-Id": event.event_id},
    )
