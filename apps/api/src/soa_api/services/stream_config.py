"""Resolve a stream's published configuration (CFG-002).

One place that answers "what config governs this stream right now": the
resolved snapshot frozen into the stream's ACTIVE published version, or
{} when the stream has never been published — callers fall back to
platform defaults, never to a draft.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.domain.streams import StreamRepository, StreamVersionRepository
from soa_db.repository import OrganizationContext


async def stream_config_by_id(
    session: AsyncSession, context: OrganizationContext, stream_id: uuid.UUID
) -> dict[str, object]:
    """The stream's active published resolved configuration, or {}."""
    stream = await StreamRepository(session, context).get(stream_id)
    if stream is None or stream.active_version_id is None:
        return {}
    active = await StreamVersionRepository(session, context).get(stream.active_version_id)
    if active is None or not active.resolved_snapshot:
        return {}
    config = active.resolved_snapshot.get("config")
    return dict(config) if isinstance(config, dict) else {}
