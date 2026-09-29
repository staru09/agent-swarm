from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from swarmguard import security
from swarmguard.api import app, timeline
from swarmguard.projector import InMemoryProjectionStore, Projector
from swarmguard.api import InMemoryQueryStore
from swarmguard.protocol import Event, EventKind


def _event(kind: EventKind, **overrides: object) -> Event:
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


async def _projected(*events: Event) -> InMemoryProjectionStore:
    store = InMemoryProjectionStore()
    projector = Projector(store)
    for item in events:
        await projector.project(item)
    return store


@pytest.fixture(autouse=True)
def real_auth_store(monkeypatch):
    # Exercise the real auth dependency (no override) against an in-memory store.
    monkeypatch.delenv("SWARMGUARD_ENV", raising=False)
    monkeypatch.delenv("SWARMGUARD_API_SIGNING_KEY", raising=False)
    original = app.dependency_overrides.copy()
    app.dependency_overrides.clear()
    timeline.query_store = InMemoryQueryStore(InMemoryProjectionStore())
    yield
    timeline.query_store = None
    app.dependency_overrides = original


def _auth(role: str = "viewer") -> dict[str, str]:
    return {"Authorization": f"Bearer {security.mint_token('tester', role)}"}


# ---------------------------------------------------------------------------
# Authentication on query endpoints
# ---------------------------------------------------------------------------


def test_query_endpoint_requires_bearer_token() -> None:
    with TestClient(app) as client:
        assert client.get("/api/runs").status_code == 401
        assert client.get("/api/events").status_code == 401


def test_query_endpoint_accepts_valid_viewer_token() -> None:
    with TestClient(app) as client:
        response = client.get("/api/runs", headers=_auth("viewer"))
        assert response.status_code == 200


def test_query_endpoint_rejects_expired_and_forged_tokens() -> None:
    import time

    expired = security.mint_token("t", "viewer", now=time.time() - 10_000, ttl_seconds=1)
    forged = security.mint_token("t", "viewer", key="attacker-key-attacker-key-attacker")
    with TestClient(app) as client:
        assert client.get("/api/runs", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
        assert client.get("/api/runs", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_higher_roles_satisfy_viewer_endpoints() -> None:
    with TestClient(app) as client:
        assert client.get("/api/runs", headers=_auth("operator")).status_code == 200
        assert client.get("/api/runs", headers=_auth("admin")).status_code == 200


def test_health_is_unauthenticated_and_non_sensitive() -> None:
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert set(body) <= {"ok", "nats", "database"}
        # Health must not leak secrets/tokens/config values.
        assert "token" not in json.dumps(body).lower()
        assert "password" not in json.dumps(body).lower()


# ---------------------------------------------------------------------------
# RBAC ladder for operator/admin scoped dependencies
# ---------------------------------------------------------------------------


def test_role_dependency_ladder_enforced() -> None:
    from swarmguard.api import require_admin, require_operator, require_viewer

    assert require_viewer.min_role == "viewer"
    assert require_operator.min_role == "operator"
    assert require_admin.min_role == "admin"


@pytest.mark.asyncio
async def test_operator_dependency_rejects_viewer_and_allows_operator() -> None:
    from fastapi import HTTPException
    from swarmguard.api import require_operator

    class Req:
        method = "POST"
        headers: dict[str, str] = {}
        url = type("U", (), {"path": "/api/ops"})()
        client = type("C", (), {"host": "127.0.0.1"})()

    viewer_req = Req()
    viewer_req.headers = {"Authorization": f"Bearer {security.mint_token('t', 'viewer')}"}
    with pytest.raises(HTTPException) as exc:
        await require_operator(viewer_req)
    assert exc.value.status_code == 403

    op_req = Req()
    op_req.headers = {"Authorization": f"Bearer {security.mint_token('t', 'operator')}"}
    claims = await require_operator(op_req)
    assert claims.role == "operator"


# ---------------------------------------------------------------------------
# WebSocket authentication
# ---------------------------------------------------------------------------


def test_websocket_without_token_is_closed_with_policy_violation() -> None:
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/live"):
                pass
        assert exc.value.code == 1008


def test_websocket_with_invalid_token_is_closed_with_policy_violation() -> None:
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/live?token=garbage"):
                pass
        assert exc.value.code == 1008


def test_websocket_with_valid_viewer_token_is_accepted() -> None:
    token = security.mint_token("t", "viewer")
    with TestClient(app) as client:
        with client.websocket_connect(f"/api/live?token={token}") as ws:
            ws.close()


def test_websocket_accepts_token_via_header() -> None:
    token = security.mint_token("t", "viewer")
    with TestClient(app) as client:
        with client.websocket_connect("/api/live", headers={"Authorization": f"Bearer {token}"}) as ws:
            ws.close()


# ---------------------------------------------------------------------------
# Access audit logging
# ---------------------------------------------------------------------------


def test_access_audit_is_emitted_without_bearer_token(caplog) -> None:
    token = security.mint_token("auditee", "viewer")
    with caplog.at_level("INFO", logger="swarmguard.access"):
        with TestClient(app) as client:
            client.get("/api/runs", headers={"Authorization": f"Bearer {token}"})
    text = caplog.text
    assert "access_audit" in text
    assert "auditee" in text
    assert token not in text
    assert "Bearer" not in text


# ---------------------------------------------------------------------------
# Redaction of secrets in API responses
# ---------------------------------------------------------------------------


async def test_api_response_redacts_secret_arguments() -> None:
    store = await _projected(
        _event(
            EventKind.TOOL_REQUESTED,
            event_id="req",
            sequence=1,
            payload={
                "tool": "web_lookup",
                "state": "requested",
                "request_id": "req-1",
                "arguments": {"url": "https://wikipedia.org", "authorization": "Bearer sk-secret-xyz"},
                "api_key": "sk-ant-secretsecretsecretsecret123456",
            },
        ),
    )
    timeline.query_store = InMemoryQueryStore(store)
    with TestClient(app) as client:
        response = client.get("/api/events", headers=_auth("viewer"))
        assert response.status_code == 200
        text = response.text
        assert "sk-secret-xyz" not in text
        assert "sk-ant-secretsecret" not in text
        assert "wikipedia.org" in text


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def test_cors_dev_allows_configured_origin() -> None:
    with TestClient(app) as client:
        response = client.get("/api/health", headers={"Origin": "https://demo.example"})
        assert response.headers.get("access-control-allow-origin") in {"*", "https://demo.example"}
