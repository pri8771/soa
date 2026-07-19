"""SSE push channel tests: auth boundary, payload-free frames, resume
cursor (JOB-007-adjacent live-update UX).

``httpx.ASGITransport`` runs the whole ASGI app to completion before
returning a response — it cannot exercise genuine client/server
concurrency, so it cannot prove "an event inserted mid-connection arrives
as a frame". These tests run the real app behind a live uvicorn server on
127.0.0.1 and connect with a normal ``httpx.AsyncClient`` over a real
socket instead.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.routers import events as events_router
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.outbox import enqueue_event

# X-Dev-User must name one of the fixed seeded demo users
# (soa_fixtures.DEMO_TENANT) — dev identity does not accept arbitrary
# strings.
ADMIN = {"X-Dev-User": "user:admin"}  # org creator -> org-admin
AUDITOR = {"X-Dev-User": "user:auditor"}  # invited into northstar only

STREAM_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    # A real 2s poll / 15s heartbeat would make every test slow; tests
    # only need the loop to run fast, not the production cadence.
    monkeypatch.setattr(events_router, "POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(events_router, "HEARTBEAT_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(events_router, "MAX_CONNECTION_SECONDS", 2.0)


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[tuple[str, DatabaseSessions, TestClient]]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/events-stream.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(ApiSettings(environment=Environment.TEST), db=db)

    sync_client = TestClient(app, raise_server_exceptions=False)
    assert (
        sync_client.post(
            "/organizations", json={"name": "Northstar", "slug": "northstar"}, headers=ADMIN
        ).status_code
        == 201
    )
    assert (
        sync_client.post(
            "/organizations", json={"name": "Southwind", "slug": "southwind"}, headers=ADMIN
        ).status_code
        == 201
    )
    sync_client.post(
        "/orgs/northstar/invitations",
        json={"email": "auditor@northstar.example"},
        headers=ADMIN,
    )
    accepted = sync_client.post(
        "/invitations/accept", json={"organization_slug": "northstar"}, headers=AUDITOR
    )
    granted = sync_client.post(
        f"/orgs/northstar/members/{accepted.json()['membership_id']}/roles",
        json={"role_slug": "auditor"},
        headers=ADMIN,
    )
    assert granted.status_code == 201, granted.text

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}", db, sync_client
    finally:
        server.should_exit = True
        await server_task


async def _read_frames(
    response: httpx.Response, *, count: int, timeout: float = STREAM_TIMEOUT
) -> list[dict[str, str]]:
    """Parse ``count`` SSE data frames from an open streaming response,
    ignoring comment/keepalive lines."""
    frames: list[dict[str, str]] = []
    current: dict[str, str] = {}

    async def _consume() -> None:
        async for raw_line in response.aiter_lines():
            line = raw_line.rstrip("\n")
            if line == "":
                if current:
                    frames.append(dict(current))
                    current.clear()
                if len(frames) >= count:
                    return
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(": ")
            current[field] = value

    await asyncio.wait_for(_consume(), timeout=timeout)
    return frames


async def test_unauthenticated_is_rejected(
    harness: tuple[str, DatabaseSessions, TestClient],
) -> None:
    base_url, _, _ = harness
    async with httpx.AsyncClient(base_url=base_url) as client:
        response = await client.get("/orgs/northstar/events/stream")
        assert response.status_code == 401


async def test_member_of_another_org_cannot_stream(
    harness: tuple[str, DatabaseSessions, TestClient],
) -> None:
    base_url, _, _ = harness
    # AUDITOR is a member of northstar only; southwind must answer exactly
    # as it would for a nonexistent org — never a 403 that would confirm
    # southwind's existence to a non-member.
    async with httpx.AsyncClient(base_url=base_url) as client:
        response = await client.get("/orgs/southwind/events/stream", headers=AUDITOR)
        assert response.status_code == 404


async def test_stream_delivers_a_payload_free_frame_for_a_new_event(
    harness: tuple[str, DatabaseSessions, TestClient],
) -> None:
    base_url, db, sync_client = harness
    org_id = uuid.UUID(sync_client.get("/orgs/northstar", headers=ADMIN).json()["id"])

    async with httpx.AsyncClient(base_url=base_url, timeout=STREAM_TIMEOUT) as client:
        async with client.stream("GET", "/orgs/northstar/events/stream", headers=ADMIN) as response:
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"

            async def _insert_after_a_beat() -> uuid.UUID:
                # Give the connection's poll loop time to resolve its
                # baseline cursor before this "new" event is created.
                await asyncio.sleep(0.15)
                async with db.session_scope() as session:
                    event = await enqueue_event(
                        session,
                        event_type="document.approved",
                        payload={"document_id": "doc-1", "secret": "should-not-leak"},
                        organization_id=org_id,
                        correlation_id="corr-123",
                    )
                    return event.id

            event_id, (frame,) = await asyncio.gather(
                _insert_after_a_beat(), _read_frames(response, count=1)
            )
            assert frame["id"] == str(event_id)
            assert frame["event"] == "document.approved"
            data = json.loads(frame["data"])
            assert data["event_type"] == "document.approved"
            assert data["correlation_id"] == "corr-123"
            assert "payload" not in data
            serialized = json.dumps(data)
            assert "document_id" not in serialized
            assert "should-not-leak" not in serialized


async def test_after_id_resume_skips_already_seen_events(
    harness: tuple[str, DatabaseSessions, TestClient],
) -> None:
    base_url, db, sync_client = harness
    org_id = uuid.UUID(sync_client.get("/orgs/northstar", headers=ADMIN).json()["id"])

    async with db.session_scope() as session:
        first = await enqueue_event(
            session, event_type="document.uploaded", payload={}, organization_id=org_id
        )
        first_id = first.id

    async with httpx.AsyncClient(base_url=base_url, timeout=STREAM_TIMEOUT) as client:
        async with client.stream(
            "GET",
            "/orgs/northstar/events/stream",
            headers=ADMIN,
            params={"after_id": str(first_id)},
        ) as response:
            assert response.status_code == 200

            async def _insert_second() -> uuid.UUID:
                await asyncio.sleep(0.15)
                async with db.session_scope() as session:
                    second = await enqueue_event(
                        session,
                        event_type="document.approved",
                        payload={},
                        organization_id=org_id,
                    )
                    return second.id

            second_id, (frame,) = await asyncio.gather(
                _insert_second(), _read_frames(response, count=1)
            )
            assert frame["id"] == str(second_id)
            assert frame["event"] == "document.approved"


async def test_connecting_with_no_after_id_only_sees_events_after_connect(
    harness: tuple[str, DatabaseSessions, TestClient],
) -> None:
    """History existing before connect must not replay — only new events."""
    base_url, db, sync_client = harness
    org_id = uuid.UUID(sync_client.get("/orgs/northstar", headers=ADMIN).json()["id"])

    async with db.session_scope() as session:
        await enqueue_event(
            session, event_type="document.uploaded", payload={}, organization_id=org_id
        )

    async with httpx.AsyncClient(base_url=base_url, timeout=STREAM_TIMEOUT) as client:
        async with client.stream("GET", "/orgs/northstar/events/stream", headers=ADMIN) as response:
            assert response.status_code == 200

            async def _insert_fresh() -> uuid.UUID:
                await asyncio.sleep(0.15)
                async with db.session_scope() as session:
                    fresh = await enqueue_event(
                        session,
                        event_type="document.approved",
                        payload={},
                        organization_id=org_id,
                    )
                    return fresh.id

            fresh_id, (frame,) = await asyncio.gather(
                _insert_fresh(), _read_frames(response, count=1)
            )
            assert frame["id"] == str(fresh_id)


async def test_invalid_after_id_is_rejected(
    harness: tuple[str, DatabaseSessions, TestClient],
) -> None:
    base_url, _, _ = harness
    async with httpx.AsyncClient(base_url=base_url) as client:
        response = await client.get(
            "/orgs/northstar/events/stream", headers=ADMIN, params={"after_id": "not-a-uuid"}
        )
        assert response.status_code == 422
