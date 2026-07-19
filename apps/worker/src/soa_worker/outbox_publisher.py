"""Authenticated HTTP publication for one durable transactional-outbox event."""

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from soa_db import DatabaseSessions
from soa_db.outbox import OutboxEvent, OutboxStatus, mark_failed, mark_published
from soa_db.outbox_destinations import get_destination
from soa_db.tenant_guard import bind_tenant
from soa_integrations import DestinationRefusedError, validate_destination


@dataclass(frozen=True)
class PublishResult:
    outcome: str  # published | retryable_error | dead_letter | no_op
    status_code: int | None = None


async def resolve_outbox_destination(
    db: DatabaseSessions,
    *,
    organization_id: uuid.UUID | None,
    global_url: str | None,
    allowlist: Sequence[str],
    resolve: Callable[[str], list[str]] | None = None,
) -> str | None:
    """Per-org destination (EXP-012) wins over the deployment-wide global
    URL when one is configured and active. System events
    (``organization_id`` is ``None``) have no tenant to look one up for and
    always use the global URL. ``resolve`` overrides DNS resolution (tests
    only) — see ``soa_integrations.validate_destination``.

    Re-validates the stored URL against the CURRENT allowlist/SSRF policy
    and fails closed — raises rather than silently falling back to the
    global receiver — if it no longer passes: a different tenant's events
    must never reach a URL nobody currently approved.
    """
    if organization_id is not None:
        async with db.session_scope() as session:
            await bind_tenant(session, organization_id)
            override = await get_destination(session, organization_id)
        if override is not None and override.is_active:
            try:
                validate_destination(override.destination_url, allowlist=allowlist, resolve=resolve)
            except DestinationRefusedError as refused:
                raise RuntimeError(
                    f"organization outbox destination failed validation: {refused}"
                ) from refused
            return override.destination_url
    return global_url


async def publish_outbox_event(
    db: DatabaseSessions,
    *,
    event_id: uuid.UUID,
    expected_organization_id: uuid.UUID | None,
    destination_url: str,
    client: httpx.AsyncClient,
    signing_secret: str | None = None,
) -> PublishResult:
    # Snapshot immutable delivery material in a short transaction, then
    # release the connection before DNS/TLS/receiver latency. The durable job
    # lease is the delivery claim; Idempotency-Key makes a crash after receiver
    # acceptance but before our final commit safe to redeliver.
    async with db.session_scope() as session:
        event = (
            await session.execute(select(OutboxEvent).where(OutboxEvent.id == event_id))
        ).scalar_one_or_none()
        if event is None:
            raise ValueError("outbox event does not exist")
        if event.organization_id != expected_organization_id:
            raise ValueError("outbox event tenant does not match its queue envelope")
        if event.status == OutboxStatus.PUBLISHED:
            return PublishResult("no_op")
        if event.status == OutboxStatus.FAILED:
            return PublishResult("dead_letter")
        envelope = {
            "id": str(event.id),
            "type": event.event_type,
            "organization_id": (str(event.organization_id) if event.organization_id else None),
            "correlation_id": event.correlation_id,
            "occurred_at": event.created_at.isoformat(),
            "payload": event.payload,
        }
    body = json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": str(event.id),
        "X-SOA-Timestamp": timestamp,
    }
    if signing_secret:
        digest = hmac.new(
            signing_secret.encode(),
            timestamp.encode() + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        headers["X-SOA-Signature"] = f"v1={digest}"
    transport_error: httpx.HTTPError | None = None
    response: httpx.Response | None = None
    try:
        response = await client.post(destination_url, content=body, headers=headers)
    except httpx.HTTPError as error:
        transport_error = error

    # Finalize under a row lock. A concurrent recovery that already reached a
    # terminal state wins; we never move published/failed rows backwards.
    async with db.session_scope() as session:
        event = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if event is None:
            raise ValueError("outbox event disappeared during publication")
        if event.organization_id != expected_organization_id:
            raise ValueError("outbox event tenant changed during publication")
        if event.status == OutboxStatus.PUBLISHED:
            return PublishResult("no_op")
        if event.status == OutboxStatus.FAILED:
            return PublishResult("dead_letter")
        if transport_error is not None:
            mark_failed(
                event,
                error=f"publication failed ({type(transport_error).__name__})",
            )
            return PublishResult(
                "dead_letter" if event.status == OutboxStatus.FAILED else "retryable_error",
                None,
            )
        assert response is not None
        if not 200 <= response.status_code < 300:
            retryable = response.status_code in (408, 425, 429) or response.status_code >= 500
            mark_failed(
                event,
                error=f"publication refused with HTTP {response.status_code}",
                terminal=not retryable,
            )
            return PublishResult(
                "retryable_error" if retryable else "dead_letter",
                response.status_code,
            )
        mark_published(event)
        return PublishResult("published", response.status_code)
