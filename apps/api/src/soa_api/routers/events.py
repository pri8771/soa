"""Server-sent-events push channel for queue/dashboard live updates.

Domain events already land in the transactional outbox
(``packages/db/src/soa_db/outbox.py``); this endpoint polls that table
server-side every ``POLL_INTERVAL_SECONDS`` and pushes a small "something
changed, refetch" notice to the browser per event — never the outbox
``payload``, which may carry document contents. Existing UI polling
(``apps/web/src/app/liveQuery.ts``) is unchanged and remains the fallback
when a stream is unavailable.

Each connection is capped at ``MAX_CONNECTION_SECONDS``; the client
reconnects with its last-seen ``after_id`` cursor, which bounds how long a
single connection (and its short-lived per-poll DB sessions) can be held
open.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import Dependencies, get_dependencies
from soa_db.outbox import OutboxEvent
from soa_db.tenant_guard import bind_tenant

router = APIRouter(tags=["events"])

#: How often the handler re-polls the outbox table for new rows.
POLL_INTERVAL_SECONDS = 2.0
#: Emit a comment frame after this long with no events so proxies/browsers
#: do not treat the connection as dead.
HEARTBEAT_INTERVAL_SECONDS = 15.0
#: Bound how long one connection (and its held resources) can live; the
#: client reconnects with its cursor, so this is invisible to the UI.
MAX_CONNECTION_SECONDS = 300.0
#: Outbox rows fetched per poll.
BATCH_LIMIT = 100


def _sse_frame(event: OutboxEvent) -> bytes:
    # Deliberately NO payload field: it may contain document contents. The
    # client only needs "something changed" to know to refetch its query.
    data = json.dumps(
        {
            "event_type": event.event_type,
            "correlation_id": event.correlation_id,
            "occurred_at": event.created_at.isoformat(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"id: {event.id}\nevent: {event.event_type}\ndata: {data}\n\n".encode()


async def _latest_event_id(deps: Dependencies, organization_id: uuid.UUID) -> uuid.UUID | None:
    assert deps.db is not None
    async with deps.db.session_scope() as session:
        await bind_tenant(session, organization_id)
        # NOT func.max(id): PostgreSQL's native uuid type has no MAX()
        # aggregate (unlike SQLite's text-backed GUID column, where it
        # works by accident). ORDER BY ... LIMIT 1 is portable and uses
        # the same btree comparison the ``id > cursor`` poll query does.
        stmt = (
            select(OutboxEvent.id)
            .where(OutboxEvent.organization_id == organization_id)
            .order_by(OutboxEvent.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar()


async def _poll_once(
    deps: Dependencies, organization_id: uuid.UUID, after: uuid.UUID | None
) -> list[OutboxEvent]:
    assert deps.db is not None
    async with deps.db.session_scope() as session:
        await bind_tenant(session, organization_id)
        stmt = select(OutboxEvent).where(OutboxEvent.organization_id == organization_id)
        if after is not None:
            stmt = stmt.where(OutboxEvent.id > after)
        stmt = stmt.order_by(OutboxEvent.id).limit(BATCH_LIMIT)
        return list((await session.execute(stmt)).scalars().all())


async def _event_stream(
    deps: Dependencies,
    organization_id: uuid.UUID,
    cursor: uuid.UUID | None,
    *,
    poll_interval_seconds: float,
    heartbeat_interval_seconds: float,
    max_connection_seconds: float,
) -> AsyncIterator[bytes]:
    # NOT ``await request.is_disconnected()``: over a real socket (verified
    # against a live uvicorn process, not just the in-process ASGI
    # transport tests use) Starlette's cancel-then-peek implementation can
    # report a fresh connection as already disconnected, closing the
    # stream within milliseconds of connecting. A genuine client
    # disconnect cancels this generator's task instead (caught below),
    # which is the reliable signal.
    if cursor is None:
        # Only NEW events, not history — an org with zero events so far
        # simply has no history to skip.
        cursor = await _latest_event_id(deps, organization_id)
    deadline = time.monotonic() + max_connection_seconds
    last_activity = time.monotonic()
    try:
        while True:
            if time.monotonic() >= deadline:
                return
            # A fresh short session every poll — never held across the sleep.
            rows = await _poll_once(deps, organization_id, cursor)
            if rows:
                for row in rows:
                    cursor = row.id
                    yield _sse_frame(row)
                last_activity = time.monotonic()
            elif time.monotonic() - last_activity >= heartbeat_interval_seconds:
                yield b": keepalive\n\n"
                last_activity = time.monotonic()
            await asyncio.sleep(poll_interval_seconds)
    except asyncio.CancelledError:
        return


@router.get("/orgs/{organization_slug}/events/stream")
async def stream_events(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.read"))],
    deps: Annotated[Dependencies, Depends(get_dependencies)],
    after_id: Annotated[
        str | None,
        Query(description="Resume cursor: the last outbox event id this client saw."),
    ] = None,
) -> StreamingResponse:
    if deps.db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not configured.",
        )
    cursor: uuid.UUID | None = None
    if after_id is not None:
        try:
            cursor = uuid.UUID(after_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="after_id must be a valid event id.",
            ) from None
    return StreamingResponse(
        _event_stream(
            deps,
            authorized.organization.id,
            cursor,
            poll_interval_seconds=POLL_INTERVAL_SECONDS,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            max_connection_seconds=MAX_CONNECTION_SECONDS,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
