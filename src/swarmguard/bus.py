from __future__ import annotations

import asyncio
import signal
import ssl
from pathlib import Path
from urllib.parse import urlparse

import nats
from nats.aio.client import Client as NATS

from . import security
from .protocol import Event, event_subject


def _build_tls_context(getenv: security.Getenv) -> tuple[ssl.SSLContext, str | None]:
    context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    ca = getenv("NATS_CA")
    if ca:
        context.load_verify_locations(cafile=ca)
    cert = getenv("NATS_CERT")
    key = getenv("NATS_KEY")
    if cert and key:
        context.load_cert_chain(certfile=cert, keyfile=key)
    url = getenv("NATS_URL") or ""
    hostname = urlparse(url).hostname
    return context, hostname


def build_nats_options(name: str, getenv: security.Getenv | None = None) -> dict[str, object]:
    """Build ``nats.connect`` options, hardening them in production.

    Development preserves the explicit local-demo behaviour (plain ``nats://``
    URL plus password auth). Production fails closed unless the connection uses
    ``tls://`` with a readable NATS credentials/NKey file, never falling back to
    password authentication.
    """
    getenv = security._getenv(getenv)
    servers = getenv("NATS_URL") or "nats://127.0.0.1:4222"
    options: dict[str, object] = {
        "servers": [servers],
        "name": name,
        "connect_timeout": 5,
        "max_reconnect_attempts": -1,
    }
    credentials = getenv("NATS_CREDS")

    if security.is_production(getenv):
        if not servers.startswith("tls://"):
            raise security.SecurityConfigError(
                "production requires a tls:// NATS_URL; got: " + servers
            )
        if not credentials:
            raise security.SecurityConfigError(
                "production requires NATS_CREDS pointing at a NATS credentials/NKey file"
            )
        if not Path(credentials).is_file():
            raise security.SecurityConfigError(
                f"production NATS_CREDS file is missing: {credentials}"
            )
        context, hostname = _build_tls_context(getenv)
        options["user_credentials"] = credentials
        options["tls"] = context
        if hostname:
            options["tls_hostname"] = hostname
        # No password fallback in production.
        return options

    if credentials:
        options["user_credentials"] = credentials
    else:
        if getenv("NATS_USER"):
            options["user"] = getenv("NATS_USER")
        if getenv("NATS_PASSWORD"):
            options["password"] = getenv("NATS_PASSWORD")
    if servers.startswith("tls://"):
        context, hostname = _build_tls_context(getenv)
        options["tls"] = context
        if hostname:
            options["tls_hostname"] = hostname
    return options


async def wait_for_shutdown() -> None:
    """Block until SIGTERM/SIGINT so callers drain NATS and atexit flushes OTel spans."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()


async def connect(name: str) -> NATS:
    return await nats.connect(**build_nats_options(name))


async def publish_event(nc: NATS, event: Event) -> None:
    # Centralized redaction before persisting to JetStream: secrets must never
    # be written into the audit log or subsequently served through the API.
    safe_event = event.model_copy(update={"payload": security.redact(event.payload)})
    await nc.publish(
        event_subject(safe_event.run_id, safe_event.kind),
        safe_event.model_dump_json().encode(),
        headers={"Nats-Msg-Id": safe_event.event_id},
    )
