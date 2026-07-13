"""Immutable export artifacts (EXP-006).

Encodes an export job's mapped payload (JSON or CSV) and stores it as a
document artifact — kind ``export_payload``, immutable like every
artifact, downloadable through the existing authorized signed-URL path
(STO-004). Storage is idempotent per (job, format): the payload is
fixed across retries (EXP-005), so re-running delivery finds the
existing artifact instead of writing a twin — and if content ever
DIFFERED for the same job, that is an invariant violation and an error,
never an overwrite.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_canonical.export_encoding import (
    ExportMetadata,
    encode_csv_export,
    encode_json_export,
)
from soa_db.artifacts import Artifact, ArtifactKind, create_artifact
from soa_db.exports import ExportJob
from soa_db.repository import OrganizationContext
from soa_storage import ObjectStore
from soa_storage.keys import artifact_key
from soa_storage.store import sha256_hex

ACTOR = "system:export"

EXPORT_FORMATS = ("json", "csv")


class ExportArtifactMismatchError(Exception):
    def __init__(self, job_id: object, export_format: str) -> None:
        super().__init__(
            f"export job {job_id} already has a {export_format} artifact with DIFFERENT "
            "content — export payloads are fixed; this is an invariant violation"
        )


@dataclass(frozen=True)
class StoredExport:
    artifact: Artifact
    reused: bool


def _stage_tag(job_id: uuid.UUID, export_format: str) -> str:
    return f"export:{job_id}:{export_format}"


async def store_export_artifact(
    session: AsyncSession,
    store: ObjectStore,
    context: OrganizationContext,
    *,
    job: ExportJob,
    export_format: str,
    payload: dict[str, Any],
    metadata: ExportMetadata,
    lines_key: str | None = None,
) -> StoredExport:
    """Encode + store the export artifact for (job, format), idempotently."""
    if export_format not in EXPORT_FORMATS:
        raise ValueError(f"export formats are {', '.join(EXPORT_FORMATS)}")
    if export_format == "json":
        content = encode_json_export(payload, metadata)
        content_type = "application/json"
        extension = "json"
    else:
        if lines_key is None:
            raise ValueError("CSV exports need the lines_key of the mapped payload")
        content = encode_csv_export(payload, metadata, lines_key=lines_key)
        content_type = "text/csv"
        extension = "csv"

    stage = _stage_tag(job.id, export_format)
    existing = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.organization_id == context.organization_id,
                    Artifact.document_id == job.document_id,
                    Artifact.kind == ArtifactKind.EXPORT_PAYLOAD.value,
                    Artifact.produced_by_stage == stage,
                )
            )
        )
        .scalars()
        .first()
    )
    digest = sha256_hex(content)
    if existing is not None:
        if existing.sha256 != digest:
            raise ExportArtifactMismatchError(job.id, export_format)
        return StoredExport(artifact=existing, reused=True)

    key = artifact_key(
        context.organization_id,
        job.document_id,
        kind="export_payload",
        filename=f"{metadata.business_key.replace(':', '-')}.{extension}",
    )
    stored = await store.put(key, content, content_type=content_type)
    artifact = await create_artifact(
        session,
        context,
        document_id=job.document_id,
        kind=ArtifactKind.EXPORT_PAYLOAD,
        object_key=key,
        sha256=stored.sha256,
        size_bytes=len(content),
        content_type=content_type,
        produced_by_run_id=job.run_id,
        produced_by_stage=stage,
        actor_id=ACTOR,
    )
    return StoredExport(artifact=artifact, reused=False)
