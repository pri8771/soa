"""Export artifact storage tests (EXP-006): immutable artifacts,
idempotent per (job, format), content mismatch refused."""

import uuid
from pathlib import Path

import pytest

from soa_canonical.export_encoding import ExportMetadata, encode_json_export
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactRepository
from soa_db.exports import create_export_job
from soa_db.repository import OrganizationContext
from soa_storage import MemoryObjectStore
from soa_worker.export_artifacts import (
    ExportArtifactMismatchError,
    store_export_artifact,
)

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
DOC = uuid.UUID("33333333-3333-4333-8333-333333333333")
RUN = uuid.UUID("55555555-5555-4555-8555-555555555555")
CONTEXT = OrganizationContext(organization_id=ORG)

METADATA = ExportMetadata(
    schema_version="1.0.0",
    mapping_version_number=1,
    integration_slug="erp",
    business_key="export:i:d:r",
    document_id=str(DOC),
    run_id=str(RUN),
    exported_at="2026-07-13T08:00:00+00:00",
)
PAYLOAD = {"PoNumber": "PO-1", "Lines": [{"Sku": "WID-100", "Qty": "10"}]}


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/export-artifacts.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_export_artifacts_are_stored_immutably_and_idempotently(
    db: DatabaseSessions,
) -> None:
    store = MemoryObjectStore()
    async with db.session_scope() as session:
        job = await create_export_job(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN,
            canonical_payload_id=uuid.uuid4(),
            integration_id=uuid.uuid4(),
            mapping_version_id=uuid.uuid4(),
            actor_id="system:test",
        )
        first = await store_export_artifact(
            session,
            store,
            CONTEXT,
            job=job,
            export_format="json",
            payload=PAYLOAD,
            metadata=METADATA,
        )
        assert first.reused is False
        assert first.artifact.kind == "export_payload"
        assert first.artifact.content_type == "application/json"
        # The stored bytes ARE the deterministic encoding.
        stored = await store.get(first.artifact.object_key)
        assert stored == encode_json_export(PAYLOAD, METADATA)

        # A retry re-encodes the SAME fixed payload: the artifact is
        # reused, not duplicated.
        again = await store_export_artifact(
            session,
            store,
            CONTEXT,
            job=job,
            export_format="json",
            payload=PAYLOAD,
            metadata=METADATA,
        )
        assert again.reused is True
        assert again.artifact.id == first.artifact.id

        # CSV is a separate artifact for the same job.
        csv_export = await store_export_artifact(
            session,
            store,
            CONTEXT,
            job=job,
            export_format="csv",
            payload=PAYLOAD,
            metadata=METADATA,
            lines_key="Lines",
        )
        assert csv_export.artifact.content_type == "text/csv"
        artifacts = await ArtifactRepository(session, CONTEXT).list_for_document(DOC)
        assert len(artifacts) == 2

        # DIFFERENT content for the same (job, format) is an invariant
        # violation, never an overwrite.
        with pytest.raises(ExportArtifactMismatchError):
            await store_export_artifact(
                session,
                store,
                CONTEXT,
                job=job,
                export_format="json",
                payload={"PoNumber": "PO-CHANGED", "Lines": []},
                metadata=METADATA,
            )

        with pytest.raises(ValueError, match="lines_key"):
            await store_export_artifact(
                session,
                store,
                CONTEXT,
                job=job,
                export_format="csv",
                payload=PAYLOAD,
                metadata=METADATA,
            )
        with pytest.raises(ValueError, match="export formats"):
            await store_export_artifact(
                session,
                store,
                CONTEXT,
                job=job,
                export_format="xml",
                payload=PAYLOAD,
                metadata=METADATA,
            )
