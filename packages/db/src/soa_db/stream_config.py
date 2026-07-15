"""Read access to a run's pinned stream configuration (CFG-002).

The ``stream_versions`` table and its lifecycle are owned by the API's
configuration domain (``soa_api.domain.streams``); the worker only ever
READS the resolved snapshot the run was pinned to. This module maps just
enough of the table for that read — on its own metadata, so it never
collides with the API's full model — because the worker must not import
``soa_api``.
"""

import uuid
from typing import Any

from sqlalchemy import Column, MetaData, Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext
from soa_db.types import GUID

_metadata = MetaData()

#: Read-only projection of the API-owned stream_versions table.
stream_versions_read = Table(
    "stream_versions",
    _metadata,
    Column("id", GUID(), primary_key=True),
    Column("organization_id", GUID(), nullable=False),
    Column("resolved_snapshot", PORTABLE_JSON, nullable=True),
)


async def pinned_stream_config(
    session: AsyncSession,
    context: OrganizationContext,
    stream_version_id: uuid.UUID,
) -> dict[str, Any]:
    """The resolved config frozen into the given published stream
    version, or {} when the version is missing (fall back to platform
    defaults, never to another tenant's row — the query is org-scoped)."""
    stmt = select(stream_versions_read.c.resolved_snapshot).where(
        stream_versions_read.c.id == stream_version_id,
        stream_versions_read.c.organization_id == context.organization_id,
    )
    snapshot = (await session.execute(stmt)).scalar_one_or_none()
    if not isinstance(snapshot, dict):
        return {}
    config = snapshot.get("config")
    return dict(config) if isinstance(config, dict) else {}
