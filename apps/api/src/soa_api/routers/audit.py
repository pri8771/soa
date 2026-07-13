"""Audit query and export (ANA-007).

- **query** — filtered, keyset-paginated reads over the DB-005 audit
  trail, gated on ``audit.read``. Sensitive-value control happens at
  WRITE time by platform convention (audit summaries carry references
  and counts, never document content or secrets); this surface adds the
  permission gate and changes nothing about what was stored.
- **export** — a bundle of NDJSON chunk files plus a manifest whose
  entries carry each file's SHA-256 and byte size, written to the
  object store and handed out ONLY as short-lived signed URLs. The
  export itself is audited (counts only). Bundles are built
  synchronously under a hard event cap; a request over the cap is
  refused with instructions to narrow the window — the queued-job path
  for unbounded exports arrives when the worker claim loop is wired.
"""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession, Dependencies, ObjectStoreDep, get_dependencies
from soa_db.audit import ActorType, AuditEvent, record_audit_event
from soa_db.types import utcnow

router = APIRouter(tags=["audit"])

MAX_PAGE = 200
#: Hard cap per export bundle; larger requests must narrow their window.
MAX_EXPORT_EVENTS = 100_000
#: Events per NDJSON chunk file inside a bundle.
EXPORT_CHUNK_EVENTS = 10_000


def _serialize(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "occurred_at": event.occurred_at.isoformat(),
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "action": event.action,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "summary": event.summary or {},
    }


def _parse_dt(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{name} must be an ISO-8601 datetime.",
        ) from None


def _filtered(
    organization_id: uuid.UUID,
    *,
    action: str | None,
    actor_id: str | None,
    target_type: str | None,
    target_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Any:
    stmt = select(AuditEvent).where(AuditEvent.organization_id == organization_id)
    if action:
        stmt = stmt.where(AuditEvent.action.like(f"{action}%"))
    if actor_id:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    if target_type:
        stmt = stmt.where(AuditEvent.target_type == target_type)
    if target_id:
        stmt = stmt.where(AuditEvent.target_id == target_id)
    if since:
        stmt = stmt.where(AuditEvent.occurred_at >= since)
    if until:
        stmt = stmt.where(AuditEvent.occurred_at < until)
    return stmt


@router.get("/orgs/{organization_slug}/audit-events")
async def list_audit_events(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("audit.read"))],
    session: DbSession,
    action: Annotated[str | None, Query(max_length=200)] = None,
    actor_id: Annotated[str | None, Query(max_length=200)] = None,
    target_type: Annotated[str | None, Query(max_length=100)] = None,
    target_id: Annotated[str | None, Query(max_length=200)] = None,
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Filtered audit trail, newest first, keyset-paginated on
    (occurred_at, id)."""
    stmt = _filtered(
        authorized.org_context.organization_id,
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        since=_parse_dt(since, "since"),
        until=_parse_dt(until, "until"),
    ).order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
    if cursor is not None:
        try:
            at_raw, _, id_raw = cursor.partition("|")
            cursor_at = datetime.fromisoformat(at_raw)
            cursor_id = uuid.UUID(id_raw)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed cursor."
            ) from None
        stmt = stmt.where(
            (AuditEvent.occurred_at < cursor_at)
            | ((AuditEvent.occurred_at == cursor_at) & (AuditEvent.id < cursor_id))
        )
    rows = list((await session.execute(stmt.limit(limit + 1))).scalars().all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = (
        f"{items[-1].occurred_at.isoformat()}|{items[-1].id}" if has_more and items else None
    )
    return {
        "items": [_serialize(event) for event in items],
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


class AuditExportRequest(BaseModel):
    action: str | None = Field(default=None, max_length=200)
    actor_id: str | None = Field(default=None, max_length=200)
    target_type: str | None = Field(default=None, max_length=100)
    target_id: str | None = Field(default=None, max_length=200)
    since: str | None = None
    until: str | None = None


@router.post("/orgs/{organization_slug}/audit-exports", status_code=status.HTTP_201_CREATED)
async def create_audit_export(
    body: AuditExportRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("audit.read"))],
    session: DbSession,
    store: ObjectStoreDep,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> dict[str, Any]:
    """Build a signed audit export bundle. See module docstring."""
    filters = {
        "action": body.action,
        "actor_id": body.actor_id,
        "target_type": body.target_type,
        "target_id": body.target_id,
        "since": body.since,
        "until": body.until,
    }
    stmt = _filtered(
        authorized.org_context.organization_id,
        action=body.action,
        actor_id=body.actor_id,
        target_type=body.target_type,
        target_id=body.target_id,
        since=_parse_dt(body.since, "since"),
        until=_parse_dt(body.until, "until"),
    ).order_by(AuditEvent.occurred_at, AuditEvent.id)
    rows = list((await session.execute(stmt.limit(MAX_EXPORT_EVENTS + 1))).scalars().all())
    if len(rows) > MAX_EXPORT_EVENTS:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"The filters match more than {MAX_EXPORT_EVENTS} events — narrow "
                "the window and export in slices."
            ),
        )

    export_id = uuid.uuid4()
    prefix = f"audit-exports/{authorized.org_context.organization_id}/{export_id}"
    ttl = deps.settings.download_url_ttl_seconds
    files: list[dict[str, Any]] = []
    for index in range(0, max(len(rows), 1), EXPORT_CHUNK_EVENTS):
        chunk = rows[index : index + EXPORT_CHUNK_EVENTS]
        name = f"events-{index // EXPORT_CHUNK_EVENTS + 1:04}.ndjson"
        payload = (
            "\n".join(
                json.dumps(_serialize(event), sort_keys=True, separators=(",", ":"))
                for event in chunk
            )
            + "\n"
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        key = f"{prefix}/{name}"
        await store.put(key, payload, content_type="application/x-ndjson", sha256=digest)
        signed = await store.signed_download_url(key, expires_in_seconds=ttl)
        files.append(
            {
                "name": name,
                "sha256": digest,
                "bytes": len(payload),
                "events": len(chunk),
                "download_url": signed.url,
                "expires_at": signed.expires_at.isoformat(),
            }
        )

    manifest = {
        "export_id": str(export_id),
        "generated_at": utcnow().isoformat(),
        "generated_by": f"user:{authorized.membership.user_id}",
        "filters": filters,
        "event_count": len(rows),
        "files": [
            {key: value for key, value in entry.items() if key not in ("download_url",)}
            for entry in files
        ],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_key = f"{prefix}/manifest.json"
    await store.put(
        manifest_key, manifest_bytes, content_type="application/json", sha256=manifest_sha256
    )
    manifest_signed = await store.signed_download_url(manifest_key, expires_in_seconds=ttl)

    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=f"user:{authorized.membership.user_id}",
        action="audit.export_created",
        target_type="audit_export",
        target_id=str(export_id),
        organization_id=authorized.org_context.organization_id,
        summary={
            "event_count": len(rows),
            "file_count": len(files),
            "manifest_sha256": manifest_sha256,
            "filters": filters,
        },
    )
    return {
        "export_id": str(export_id),
        "event_count": len(rows),
        "manifest_sha256": manifest_sha256,
        "manifest_download_url": manifest_signed.url,
        "manifest_expires_at": manifest_signed.expires_at.isoformat(),
        "files": files,
    }
