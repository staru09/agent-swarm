from __future__ import annotations

import argparse
import os
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from nats.errors import Error as NATSError
from pydantic import ValidationError

from .bus import connect
from .protocol import Event


class Timeline:
    def __init__(self):
        self.events: deque[Event] = deque(maxlen=10_000)
        self.clients: set[WebSocket] = set()
        self.nc = None
        self.subscription = None

    async def start(self) -> None:
        self.nc = await connect("timeline-api")
        js = self.nc.jetstream()
        try:
            # Request/reply subjects must remain Core NATS. Putting them in a
            # stream would send a JetStream publish acknowledgement to the
            # request inbox before the gateway's actual ToolResponse.
            await js.add_stream(name="SWARM_EVENTS", subjects=["audit.>"])
        except NATSError:
            await js.update_stream(name="SWARM_EVENTS", subjects=["audit.>"])
        self.subscription = await js.subscribe("audit.>", ordered_consumer=True, cb=self._receive)

    async def stop(self) -> None:
        if self.subscription:
            await self.subscription.unsubscribe()
        if self.nc:
            await self.nc.drain()

    async def _receive(self, msg) -> None:
        try:
            event = Event.model_validate_json(msg.data)
        except ValidationError:
            await msg.ack()
            return
        self.events.append(event)
        stale: list[WebSocket] = []
        for client in self.clients:
            try:
                await client.send_text(event.model_dump_json())
            except Exception:
                stale.append(client)
        self.clients.difference_update(stale)
        await msg.ack()


timeline = Timeline()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await timeline.start()
    try:
        yield
    finally:
        await timeline.stop()


app = FastAPI(title="SwarmGuard", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "nats": bool(timeline.nc and timeline.nc.is_connected)}


@app.get("/api/events")
async def events(run_id: str | None = None) -> list[dict]:
    values = timeline.events
    if run_id:
        return [event.model_dump(mode="json") for event in values if event.run_id == run_id]
    return [event.model_dump(mode="json") for event in values]


@app.websocket("/api/live")
async def live(websocket: WebSocket) -> None:
    await websocket.accept()
    timeline.clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        timeline.clients.discard(websocket)


DIST = Path(
    os.getenv(
        "SWARMGUARD_UI_DIR",
        str(Path(__file__).resolve().parents[2] / "frontend" / "dist"),
    )
)
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    async def frontend(path: str):
        requested = DIST / path
        if path and requested.is_file():
            return FileResponse(requested)
        return FileResponse(DIST / "index.html")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("API_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "8000")))
    args = parser.parse_args()
    uvicorn.run("swarmguard.api:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
