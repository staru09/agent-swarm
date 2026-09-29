from __future__ import annotations

import json

import pytest

from swarmguard import security
from swarmguard.bus import publish_event
from swarmguard.protocol import Event, EventKind


class CapturingNats:
    def __init__(self) -> None:
        self.messages: list[bytes] = []

    async def publish(self, subject: str, data: bytes, headers: dict | None = None) -> None:
        self.messages.append(data)


@pytest.mark.asyncio
async def test_publish_event_redacts_secrets_before_persist() -> None:
    nc = CapturingNats()
    event = Event(
        run_id="run-1",
        kind=EventKind.TOOL_REQUESTED,
        payload={
            "tool": "web_lookup",
            "state": "requested",
            "arguments": {"url": "https://wikipedia.org"},
            "authorization": "Bearer sk-secret-123",
            "nested": {"password": "hunter2", "api_key": "sk-ant-abcdefghijklmnopqrstuvwx"},
        },
    )

    await publish_event(nc, event)

    text = nc.messages[0].decode()
    assert "sk-secret-123" not in text
    assert "hunter2" not in text
    assert "sk-ant-abcdefghijklmnopqrstuvwx" not in text
    # Non-sensitive values survive.
    assert "wikipedia.org" in text
    parsed = json.loads(text)
    assert parsed["payload"]["authorization"] == security.REDACTION_PLACEHOLDER
    assert parsed["payload"]["nested"]["password"] == security.REDACTION_PLACEHOLDER


@pytest.mark.asyncio
async def test_publish_event_does_not_mutate_original_event() -> None:
    nc = CapturingNats()
    event = Event(
        run_id="run-1",
        kind=EventKind.TOOL_REQUESTED,
        payload={"password": "hunter2"},
    )
    await publish_event(nc, event)
    assert event.payload["password"] == "hunter2"


def test_encrypted_retention_hides_plaintext_and_survives_round_trip() -> None:
    key = b"k" * 32
    envelope = security.encrypt_sensitive("classified-report-contents", key=key)
    serialized = json.dumps(envelope)
    assert "classified-report-contents" not in serialized
    assert security.decrypt_sensitive(envelope, key=key) == "classified-report-contents"


def test_tampered_encrypted_retention_fails_closed() -> None:
    key = b"k" * 32
    envelope = security.encrypt_sensitive("classified", key=key)
    raw = bytearray(security._b64url_decode(envelope["ciphertext"]))
    raw[-1] ^= 0x01
    envelope["ciphertext"] = security._b64url_encode(bytes(raw))
    with pytest.raises(security.DecryptionError):
        security.decrypt_sensitive(envelope, key=key)
