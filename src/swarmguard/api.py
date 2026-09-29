from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from nats.errors import Error as NATSError
from pydantic import BaseModel, ValidationError

from . import security
from .bus import connect
from .projector import InMemoryProjectionStore
from .protocol import Event, EventKind


access_logger = logging.getLogger("swarmguard.access")


def _emit_access(request: Any, claims: Any, decision: str, status: int) -> dict[str, Any]:
    record = security.access_audit_record(
        method=getattr(request, "method", "WEBSOCKET"),
        path=getattr(getattr(request, "url", None), "path", "") or "",
        subject=getattr(claims, "sub", None),
        role=getattr(claims, "role", None),
        decision=decision,
        status=status,
        client=getattr(getattr(request, "client", None), "host", None),
    )
    access_logger.info(json.dumps(record, default=str))
    return record


def _extract_bearer_token(headers: Any, query_token: str | None = None) -> str:
    auth = ""
    try:
        auth = headers.get("Authorization") or headers.get("authorization") or ""
    except AttributeError:
        auth = ""
    if isinstance(auth, str) and auth.startswith("Bearer "):
        return auth[len("Bearer ") :].strip()
    return query_token or ""


class _RoleChecker:
    """Stable, overridable FastAPI dependency enforcing a minimum role.

    Instances are module-level singletons so tests can override them via
    ``app.dependency_overrides`` and so the enforced ``min_role`` is inspectable.
    """

    def __init__(self, min_role: str):
        self.min_role = min_role

    async def __call__(self, request: Request) -> security.TokenClaims:
        token = _extract_bearer_token(request.headers)
        try:
            claims = security.verify_token(token)
        except security.AuthError as exc:
            _emit_access(request, None, "deny", 401)
            raise HTTPException(status_code=401, detail="authentication required") from exc
        if not security.role_satisfies(claims.role, self.min_role):
            _emit_access(request, claims, "deny", 403)
            raise HTTPException(status_code=403, detail="insufficient role")
        _emit_access(request, claims, "allow", 200)
        return claims


require_viewer = _RoleChecker("viewer")
require_operator = _RoleChecker("operator")
require_admin = _RoleChecker("admin")


def _redact_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [security.redact(item) for item in items]


def _redact_page(page: "Page") -> "Page":
    return Page(items=_redact_items(page.items), next_cursor=page.next_cursor)


MAX_PAGE_LIMIT = 100
StateFilter = Literal[
    "active",
    "requested",
    "policy_decided",
    "allowed",
    "dispatched",
    "started",
    "heartbeat",
    "completed",
    "failed",
    "denied",
    "timed_out",
    "cancelled",
    "exhausted",
    "dead_letter",
]


class QueryStoreUnavailable(RuntimeError):
    pass


class PageParams(BaseModel):
    limit: int
    cursor: str | None = None


class Page(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class RunDetail(BaseModel):
    run: dict[str, Any]
    agents: list[dict[str, Any]]
    model_steps: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    tool_attempts: list[dict[str, Any]]
    policy_decisions: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]


class TraceDetail(BaseModel):
    trace_id: str
    events: list[dict[str, Any]]
    tool_attempts: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]


class QueryStore(Protocol):
    async def list_runs(self, page: PageParams) -> Page: ...
    async def get_run_detail(self, run_id: str) -> RunDetail | None: ...
    async def list_agents(self, run_id: str, page: PageParams, state: str | None = None) -> Page: ...
    async def list_model_steps(self, run_id: str, page: PageParams, state: str | None = None) -> Page: ...
    async def list_tool_calls(
        self, run_id: str, page: PageParams, state: str | None = None, tool: str | None = None
    ) -> Page: ...
    async def list_tool_attempts(self, run_id: str, tool_call_id: str, page: PageParams) -> Page: ...
    async def list_policy_decisions(self, run_id: str, page: PageParams, decision: str | None = None) -> Page: ...
    async def list_artifacts(self, run_id: str, page: PageParams, kind: str | None = None) -> Page: ...
    async def list_events(
        self,
        page: PageParams,
        *,
        run_id: str | None = None,
        kind: EventKind | None = None,
        after_cursor: str | None = None,
    ) -> Page: ...
    async def get_trace(self, trace_id: str) -> TraceDetail | None: ...


def page_params(limit: int = Query(50, ge=1, le=MAX_PAGE_LIMIT), cursor: str | None = None) -> PageParams:
    return PageParams(limit=limit, cursor=cursor)


def _event_cursor(row: dict[str, Any]) -> str:
    return f"{row.get('sequence', 0)}:{row['event_id']}"


def _parse_event_cursor(cursor: str | None) -> tuple[int, str] | None:
    if not cursor:
        return None
    sequence, _, event_id = cursor.partition(":")
    try:
        return int(sequence), event_id
    except ValueError as exc:
        raise ValueError("invalid cursor") from exc


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _state_filter(rows: list[dict[str, Any]], state: str | None) -> list[dict[str, Any]]:
    return [row for row in rows if state is None or row.get("state") == state]


def _page_rows(rows: list[dict[str, Any]], page: PageParams, *, key: str) -> Page:
    start = 0
    if page.cursor:
        values = [str(row[key]) for row in rows]
        if page.cursor in values:
            start = values.index(page.cursor) + 1
    selected = rows[start : start + page.limit + 1]
    items = [_jsonable(row) for row in selected[: page.limit]]
    next_cursor = str(selected[page.limit - 1][key]) if len(selected) > page.limit else None
    return Page(items=items, next_cursor=next_cursor)


def _page_events(rows: list[dict[str, Any]], page: PageParams) -> Page:
    selected = rows[: page.limit + 1]
    items = [_jsonable(row) for row in selected[: page.limit]]
    next_cursor = _event_cursor(selected[page.limit - 1]) if len(selected) > page.limit else None
    return Page(items=items, next_cursor=next_cursor)


def _with_projection_fields(row: dict[str, Any], **extra: Any) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return {
        **row,
        "event_id": row.get("event_id") or row.get("last_event_id"),
        "latency_ms": payload.get("latency_ms"),
        "policy_version": payload.get("policy_version"),
        "linked_kernel_evidence": payload.get("kernel_trace_id"),
        **extra,
    }


def _event_row(row: dict[str, Any]) -> dict[str, Any]:
    return _with_projection_fields(row, linked_kernel_evidence=(row.get("payload") or {}).get("kernel_trace_id"))


class InMemoryQueryStore:
    def __init__(self, store: InMemoryProjectionStore):
        self.store = store

    async def list_runs(self, page: PageParams) -> Page:
        rows = sorted((_with_projection_fields(row) for row in self.store.runs.values()), key=lambda row: row["run_id"])
        return _page_rows(rows, page, key="run_id")

    async def get_run_detail(self, run_id: str) -> RunDetail | None:
        run = self.store.runs.get(run_id)
        if run is None:
            return None
        page = PageParams(limit=MAX_PAGE_LIMIT)
        agents = (await self.list_agents(run_id, page)).items
        model_steps = (await self.list_model_steps(run_id, page)).items
        tool_calls = (await self.list_tool_calls(run_id, page)).items
        attempts: list[dict[str, Any]] = []
        for call in tool_calls:
            attempts.extend((await self.list_tool_attempts(run_id, str(call["tool_call_id"]), page)).items)
        policies = (await self.list_policy_decisions(run_id, page)).items
        artifacts = (await self.list_artifacts(run_id, page)).items
        return RunDetail(
            run=_jsonable(_with_projection_fields(run, run_id=run_id)),
            agents=agents,
            model_steps=model_steps,
            tool_calls=tool_calls,
            tool_attempts=attempts,
            policy_decisions=policies,
            artifacts=artifacts,
        )

    async def list_agents(self, run_id: str, page: PageParams, state: str | None = None) -> Page:
        rows = [
            _with_projection_fields(row, run_id=key[0], agent_instance_id=key[2])
            for key, row in self.store.agent_instances.items()
            if key[0] == run_id
        ]
        rows = sorted(_state_filter(rows, state), key=lambda row: (row["created_at"], row["agent_instance_id"]))
        return _page_rows(rows, page, key="agent_instance_id")

    async def list_model_steps(self, run_id: str, page: PageParams, state: str | None = None) -> Page:
        rows = [
            _with_projection_fields(row, run_id=key[0], agent_instance_id=key[1], step_id=key[2])
            for key, row in self.store.model_steps.items()
            if key[0] == run_id
        ]
        rows = sorted(_state_filter(rows, state), key=lambda row: (row["created_at"], row["step_id"]))
        return _page_rows(rows, page, key="step_id")

    async def list_tool_calls(
        self, run_id: str, page: PageParams, state: str | None = None, tool: str | None = None
    ) -> Page:
        rows = [
            _with_projection_fields(row, run_id=key[0], tool_call_id=key[1])
            for key, row in self.store.tool_calls.items()
            if key[0] == run_id and (tool is None or row.get("tool") == tool)
        ]
        rows = sorted(_state_filter(rows, state), key=lambda row: (row["created_at"], row["tool_call_id"]))
        return _page_rows(rows, page, key="tool_call_id")

    async def list_tool_attempts(self, run_id: str, tool_call_id: str, page: PageParams) -> Page:
        rows = [
            _with_projection_fields(row, run_id=key[0], tool_call_id=key[1], attempt=key[2])
            for key, row in self.store.tool_attempts.items()
            if key[0] == run_id and key[1] == tool_call_id
        ]
        rows = sorted(rows, key=lambda row: row["attempt"])
        return _page_rows(rows, page, key="attempt")

    async def list_policy_decisions(self, run_id: str, page: PageParams, decision: str | None = None) -> Page:
        rows = [
            _with_projection_fields(row, run_id=key[0], tool_call_id=key[1], event_id=key[2])
            for key, row in self.store.policy_decisions.items()
            if key[0] == run_id and (decision is None or row.get("decision") == decision)
        ]
        rows = sorted(rows, key=lambda row: (row["created_at"], row["event_id"]))
        return _page_rows(rows, page, key="event_id")

    async def list_artifacts(self, run_id: str, page: PageParams, kind: str | None = None) -> Page:
        rows = [
            _with_projection_fields(row, linked_kernel_evidence=row.get("payload", {}).get("kernel_trace_id"))
            for row in self.store.artifacts.values()
            if row["run_id"] == run_id and (kind is None or row.get("kind") == kind)
        ]
        rows = sorted(rows, key=lambda row: (row["created_at"], row["artifact_id"]))
        return _page_rows(rows, page, key="artifact_id")

    async def list_events(
        self,
        page: PageParams,
        *,
        run_id: str | None = None,
        kind: EventKind | None = None,
        after_cursor: str | None = None,
    ) -> Page:
        after = _parse_event_cursor(after_cursor or page.cursor)
        rows = [
            _event_row(row)
            for row in self.store.events.values()
            if (run_id is None or row["run_id"] == run_id) and (kind is None or row["kind"] == kind.value)
        ]
        rows = sorted(rows, key=lambda row: (row["sequence"], row["event_id"]))
        if after is not None:
            rows = [row for row in rows if (row["sequence"], row["event_id"]) > after]
        return _page_events(rows, PageParams(limit=page.limit))

    async def get_trace(self, trace_id: str) -> TraceDetail | None:
        events = sorted(
            [row for row in self.store.events.values() if row.get("trace_id") == trace_id],
            key=lambda row: (row["sequence"], row["event_id"]),
        )
        if not events:
            return None
        run_ids = {row["run_id"] for row in events}
        tool_call_ids = {row["tool_call_id"] for row in events if row.get("tool_call_id")}
        attempts = [
            _with_projection_fields(row, run_id=key[0], tool_call_id=key[1], attempt=key[2])
            for key, row in self.store.tool_attempts.items()
            if key[0] in run_ids and key[1] in tool_call_ids
        ]
        artifacts = [
            _with_projection_fields(row, linked_kernel_evidence=row.get("payload", {}).get("kernel_trace_id"))
            for row in self.store.artifacts.values()
            if row["run_id"] in run_ids
        ]
        return TraceDetail(
            trace_id=trace_id,
            events=[_jsonable(row) for row in events],
            tool_attempts=[_jsonable(row) for row in sorted(attempts, key=lambda row: row["attempt"])],
            artifacts=[_jsonable(row) for row in sorted(artifacts, key=lambda row: row["artifact_id"])],
        )


class AsyncPostgresQueryStore:
    def __init__(self, pool: Any):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str | None = None) -> "AsyncPostgresQueryStore":
        import asyncpg

        try:
            pool = await asyncpg.create_pool(dsn or os.environ["DATABASE_URL"])
        except Exception as exc:
            raise QueryStoreUnavailable("database unavailable") from exc
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def _fetch_page(self, sql: str, *args: Any, page: PageParams, key: str) -> Page:
        try:
            rows = [dict(row) for row in await self.pool.fetch(sql, *args, page.limit + 1)]
        except Exception as exc:
            raise QueryStoreUnavailable("database unavailable") from exc
        return _page_rows([self._decode(row) for row in rows], page, key=key)

    def _decode(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = row.get("payload")
        if isinstance(payload, str):
            row["payload"] = json.loads(payload)
        return _with_projection_fields(row, linked_kernel_evidence=(row.get("payload") or {}).get("kernel_trace_id"))

    async def list_runs(self, page: PageParams) -> Page:
        return await self._fetch_page(
            """
            SELECT *, run_id AS cursor_key FROM runs
            WHERE ($1::text IS NULL OR run_id > $1)
            ORDER BY run_id
            LIMIT $2
            """,
            page.cursor,
            page=page,
            key="run_id",
        )

    async def get_run_detail(self, run_id: str) -> RunDetail | None:
        runs = await self._fetch_page("SELECT * FROM runs WHERE run_id = $1 LIMIT $2", run_id, page=PageParams(limit=1), key="run_id")
        if not runs.items:
            return None
        page = PageParams(limit=MAX_PAGE_LIMIT)
        return RunDetail(
            run=runs.items[0],
            agents=(await self.list_agents(run_id, page)).items,
            model_steps=(await self.list_model_steps(run_id, page)).items,
            tool_calls=(await self.list_tool_calls(run_id, page)).items,
            tool_attempts=[self._decode(dict(row)) for row in await self.pool.fetch("SELECT * FROM tool_attempts WHERE run_id = $1 ORDER BY tool_call_id, attempt LIMIT $2", run_id, MAX_PAGE_LIMIT)],
            policy_decisions=(await self.list_policy_decisions(run_id, page)).items,
            artifacts=(await self.list_artifacts(run_id, page)).items,
        )

    async def list_agents(self, run_id: str, page: PageParams, state: str | None = None) -> Page:
        return await self._fetch_page(
            "SELECT * FROM agent_instances WHERE run_id = $1 AND ($2::text IS NULL OR state = $2) AND ($3::text IS NULL OR agent_instance_id > $3) ORDER BY agent_instance_id LIMIT $4",
            run_id,
            state,
            page.cursor,
            page=page,
            key="agent_instance_id",
        )

    async def list_model_steps(self, run_id: str, page: PageParams, state: str | None = None) -> Page:
        return await self._fetch_page(
            "SELECT * FROM model_steps WHERE run_id = $1 AND ($2::text IS NULL OR state = $2) AND ($3::text IS NULL OR step_id > $3) ORDER BY step_id LIMIT $4",
            run_id,
            state,
            page.cursor,
            page=page,
            key="step_id",
        )

    async def list_tool_calls(
        self, run_id: str, page: PageParams, state: str | None = None, tool: str | None = None
    ) -> Page:
        return await self._fetch_page(
            "SELECT * FROM tool_calls WHERE run_id = $1 AND ($2::text IS NULL OR state = $2) AND ($3::text IS NULL OR tool = $3) AND ($4::text IS NULL OR tool_call_id > $4) ORDER BY tool_call_id LIMIT $5",
            run_id,
            state,
            tool,
            page.cursor,
            page=page,
            key="tool_call_id",
        )

    async def list_tool_attempts(self, run_id: str, tool_call_id: str, page: PageParams) -> Page:
        return await self._fetch_page(
            "SELECT * FROM tool_attempts WHERE run_id = $1 AND tool_call_id = $2 AND ($3::int IS NULL OR attempt > $3) ORDER BY attempt LIMIT $4",
            run_id,
            tool_call_id,
            int(page.cursor) if page.cursor else None,
            page=page,
            key="attempt",
        )

    async def list_policy_decisions(self, run_id: str, page: PageParams, decision: str | None = None) -> Page:
        return await self._fetch_page(
            "SELECT * FROM policy_decisions WHERE run_id = $1 AND ($2::text IS NULL OR decision = $2) AND ($3::text IS NULL OR event_id > $3) ORDER BY event_id LIMIT $4",
            run_id,
            decision,
            page.cursor,
            page=page,
            key="event_id",
        )

    async def list_artifacts(self, run_id: str, page: PageParams, kind: str | None = None) -> Page:
        return await self._fetch_page(
            "SELECT * FROM artifacts WHERE run_id = $1 AND ($2::text IS NULL OR kind = $2) AND ($3::text IS NULL OR artifact_id > $3) ORDER BY artifact_id LIMIT $4",
            run_id,
            kind,
            page.cursor,
            page=page,
            key="artifact_id",
        )

    async def list_events(
        self,
        page: PageParams,
        *,
        run_id: str | None = None,
        kind: EventKind | None = None,
        after_cursor: str | None = None,
    ) -> Page:
        after = _parse_event_cursor(after_cursor or page.cursor)
        after_sequence = after[0] if after else None
        after_event_id = after[1] if after else None
        try:
            rows = [
                self._decode(dict(row))
                for row in await self.pool.fetch(
                    """
                    SELECT * FROM events
                    WHERE ($1::text IS NULL OR run_id = $1)
                      AND ($2::text IS NULL OR kind = $2)
                      AND ($3::bigint IS NULL OR (sequence, event_id) > ($3, $4))
                    ORDER BY sequence, event_id
                    LIMIT $5
                    """,
                    run_id,
                    kind.value if kind else None,
                    after_sequence,
                    after_event_id,
                    page.limit + 1,
                )
            ]
        except Exception as exc:
            raise QueryStoreUnavailable("database unavailable") from exc
        return _page_events(rows, PageParams(limit=page.limit))

    async def get_trace(self, trace_id: str) -> TraceDetail | None:
        try:
            events = [self._decode(dict(row)) for row in await self.pool.fetch("SELECT * FROM events WHERE trace_id = $1 ORDER BY sequence, event_id", trace_id)]
            if not events:
                return None
            attempts = [self._decode(dict(row)) for row in await self.pool.fetch("SELECT DISTINCT ta.* FROM tool_attempts ta JOIN events e ON e.run_id = ta.run_id AND e.tool_call_id = ta.tool_call_id WHERE e.trace_id = $1 ORDER BY ta.tool_call_id, ta.attempt", trace_id)]
            artifacts = [self._decode(dict(row)) for row in await self.pool.fetch("SELECT DISTINCT a.* FROM artifacts a JOIN events e ON e.run_id = a.run_id WHERE e.trace_id = $1 ORDER BY a.artifact_id", trace_id)]
        except Exception as exc:
            raise QueryStoreUnavailable("database unavailable") from exc
        return TraceDetail(trace_id=trace_id, events=events, tool_attempts=attempts, artifacts=artifacts)


@dataclass
class LiveClient:
    websocket: Any
    replaying: bool = True
    # Dedupe is scoped to a single WebSocket connection. Bounding this earlier
    # would allow a long catch-up replay to redeliver an old event on reconnect.
    sent: set[str] = field(default_factory=set)
    buffered: list[Event] = field(default_factory=list)

    async def deliver(self, event: Event) -> None:
        if event.event_id in self.sent:
            return
        if self.replaying:
            self.buffered.append(event)
            return
        await self.send(event)

    async def send(self, event: Event) -> None:
        if event.event_id in self.sent:
            return
        await self.websocket.send_text(event.model_dump_json())
        self.sent.add(event.event_id)

    async def finish_replay(self) -> None:
        self.replaying = False
        for event in sorted(self.buffered, key=lambda item: (item.sequence, item.event_id)):
            await self.send(event)
        self.buffered.clear()


class Timeline:
    def __init__(self):
        self.live_buffer: deque[Event] = deque(maxlen=1_000)
        self.clients: dict[Any, LiveClient] = {}
        self.nc = None
        self.subscription = None
        self.query_store: QueryStore | None = None

    async def start(self) -> None:
        try:
            if self.query_store is None:
                if os.getenv("DATABASE_URL"):
                    self.query_store = await AsyncPostgresQueryStore.connect(os.getenv("DATABASE_URL"))
                else:
                    self.query_store = InMemoryQueryStore(InMemoryProjectionStore())
            self.nc = await asyncio.wait_for(connect("timeline-api"), timeout=1.0)
            js = self.nc.jetstream()
            # Binding the stream by name avoids a STREAM.NAMES lookup the timeline ACL does not grant.
            self.subscription = await js.subscribe(
                "audit.>", stream="SWARMGUARD_AUDIT", ordered_consumer=True, cb=self._receive
            )
        except (Exception, NATSError):
            self.nc = None

    async def stop(self) -> None:
        if self.subscription:
            await self.subscription.unsubscribe()
        if self.nc:
            await self.nc.drain()
        if isinstance(self.query_store, AsyncPostgresQueryStore):
            await self.query_store.close()

    async def _receive(self, msg) -> None:
        try:
            event = Event.model_validate_json(msg.data)
        except ValidationError:
            await msg.ack()
            return
        self.live_buffer.append(event)
        stale: list[Any] = []
        for websocket, client in list(self.clients.items()):
            try:
                await client.deliver(event)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.clients.pop(websocket, None)
        await msg.ack()

    async def register_with_catch_up(self, websocket: Any, last_cursor: str | None) -> None:
        client = LiveClient(websocket)
        self.clients[websocket] = client
        cursor = last_cursor
        while self.query_store is not None and cursor:
            page = await self.query_store.list_events(PageParams(limit=MAX_PAGE_LIMIT), after_cursor=cursor)
            for item in page.items:
                await client.send(Event.model_validate(item))
            cursor = page.next_cursor
        await client.finish_replay()

    def unregister(self, websocket: Any) -> None:
        self.clients.pop(websocket, None)


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
    allow_origins=security.resolve_cors_origins(),
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.exception_handler(QueryStoreUnavailable)
async def query_store_exception_handler(_, exc: QueryStoreUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/api/health")
async def health() -> dict:
    if isinstance(timeline.query_store, AsyncPostgresQueryStore):
        database = {"durable": True, "backend": "postgres"}
    elif isinstance(timeline.query_store, InMemoryQueryStore):
        database = {"durable": False, "backend": "memory"}
    else:
        database = {"durable": False, "backend": "absent"}
    return {"ok": True, "nats": bool(timeline.nc and timeline.nc.is_connected), "database": database}


async def get_query_store() -> QueryStore:
    if timeline.query_store is None:
        timeline.query_store = InMemoryQueryStore(InMemoryProjectionStore())
    return timeline.query_store


def _service_unavailable(exc: QueryStoreUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@app.get("/api/runs", response_model=Page)
async def runs(
    page: PageParams = Depends(page_params),
    store: QueryStore = Depends(get_query_store),
    _claims: security.TokenClaims = Depends(require_viewer),
) -> Page:
    try:
        return _redact_page(await store.list_runs(page))
    except QueryStoreUnavailable as exc:
        raise _service_unavailable(exc) from exc


@app.get("/api/runs/{run_id}", response_model=RunDetail)
async def run_detail(
    run_id: str,
    store: QueryStore = Depends(get_query_store),
    _claims: security.TokenClaims = Depends(require_viewer),
) -> RunDetail:
    try:
        detail = await store.get_run_detail(run_id)
    except QueryStoreUnavailable as exc:
        raise _service_unavailable(exc) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="run not found")
    return RunDetail(
        run=security.redact(detail.run),
        agents=_redact_items(detail.agents),
        model_steps=_redact_items(detail.model_steps),
        tool_calls=_redact_items(detail.tool_calls),
        tool_attempts=_redact_items(detail.tool_attempts),
        policy_decisions=_redact_items(detail.policy_decisions),
        artifacts=_redact_items(detail.artifacts),
    )


@app.get("/api/runs/{run_id}/agents", response_model=Page)
async def agent_instances(
    run_id: str,
    state: StateFilter | None = None,
    page: PageParams = Depends(page_params),
    store: QueryStore = Depends(get_query_store),
    _claims: security.TokenClaims = Depends(require_viewer),
) -> Page:
    return _redact_page(await store.list_agents(run_id, page, state))


@app.get("/api/runs/{run_id}/model-steps", response_model=Page)
async def model_steps(
    run_id: str,
    state: StateFilter | None = None,
    page: PageParams = Depends(page_params),
    store: QueryStore = Depends(get_query_store),
    _claims: security.TokenClaims = Depends(require_viewer),
) -> Page:
    return _redact_page(await store.list_model_steps(run_id, page, state))


@app.get("/api/runs/{run_id}/tool-calls", response_model=Page)
async def tool_calls(
    run_id: str,
    state: StateFilter | None = None,
    tool: str | None = None,
    page: PageParams = Depends(page_params),
    store: QueryStore = Depends(get_query_store),
    _claims: security.TokenClaims = Depends(require_viewer),
) -> Page:
    return _redact_page(await store.list_tool_calls(run_id, page, state, tool))


@app.get("/api/runs/{run_id}/tool-calls/{tool_call_id}/attempts", response_model=Page)
async def tool_call_attempts(
    run_id: str,
    tool_call_id: str,
    page: PageParams = Depends(page_params),
    store: QueryStore = Depends(get_query_store),
    _claims: security.TokenClaims = Depends(require_viewer),
) -> Page:
    return _redact_page(await store.list_tool_attempts(run_id, tool_call_id, page))


@app.get("/api/runs/{run_id}/policy-decisions", response_model=Page)
async def policy_decisions(
    run_id: str,
    decision: str | None = Query(None, pattern="^(allowed|denied)$"),
    page: PageParams = Depends(page_params),
    store: QueryStore = Depends(get_query_store),
    _claims: security.TokenClaims = Depends(require_viewer),
) -> Page:
    return _redact_page(await store.list_policy_decisions(run_id, page, decision))


@app.get("/api/runs/{run_id}/artifacts", response_model=Page)
async def artifacts(
    run_id: str,
    kind: str | None = None,
    page: PageParams = Depends(page_params),
    store: QueryStore = Depends(get_query_store),
    _claims: security.TokenClaims = Depends(require_viewer),
) -> Page:
    return _redact_page(await store.list_artifacts(run_id, page, kind))


@app.get("/api/events", response_model=Page)
async def events(
    run_id: str | None = None,
    kind: EventKind | None = None,
    page: PageParams = Depends(page_params),
    store: QueryStore = Depends(get_query_store),
    _claims: security.TokenClaims = Depends(require_viewer),
) -> Page:
    try:
        return _redact_page(await store.list_events(page, run_id=run_id, kind=kind))
    except QueryStoreUnavailable as exc:
        raise _service_unavailable(exc) from exc


@app.get("/api/traces/{trace_id}", response_model=TraceDetail)
async def trace_detail(
    trace_id: str,
    store: QueryStore = Depends(get_query_store),
    _claims: security.TokenClaims = Depends(require_viewer),
) -> TraceDetail:
    try:
        detail = await store.get_trace(trace_id)
    except QueryStoreUnavailable as exc:
        raise _service_unavailable(exc) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return TraceDetail(
        trace_id=detail.trace_id,
        events=_redact_items(detail.events),
        tool_attempts=_redact_items(detail.tool_attempts),
        artifacts=_redact_items(detail.artifacts),
    )


async def replay_after_cursor(query_store: QueryStore, *, last_cursor: str | None, live_events: list[Event]) -> list[Event]:
    page = await query_store.list_events(PageParams(limit=MAX_PAGE_LIMIT), after_cursor=last_cursor)
    events = [Event.model_validate(item) for item in page.items]
    seen = {event.event_id for event in events}
    for event in live_events:
        if event.event_id not in seen:
            events.append(event)
            seen.add(event.event_id)
    return events


@app.websocket("/api/live")
async def live(websocket: WebSocket) -> None:
    # Authenticate BEFORE accepting the connection. Unauthorized clients are
    # closed with a policy-violation code (1008) and never see live audit data.
    token = _extract_bearer_token(websocket.headers, websocket.query_params.get("token"))
    try:
        claims = security.verify_token(token)
        if not security.role_satisfies(claims.role, "viewer"):
            raise security.AuthError("insufficient role for live feed")
    except security.AuthError:
        _emit_access(websocket, None, "deny", 1008)
        await websocket.close(code=1008)
        return
    _emit_access(websocket, claims, "allow", 101)
    await websocket.accept()
    last_cursor = websocket.query_params.get("cursor")
    await timeline.register_with_catch_up(websocket, last_cursor)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        timeline.unregister(websocket)


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
    parser.add_argument("--host", default=security.resolve_api_bind_host())
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "8000")))
    args = parser.parse_args()
    uvicorn.run("swarmguard.api:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
