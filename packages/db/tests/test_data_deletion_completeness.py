"""Regression coverage for every document-linked persistence category."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, create_artifact
from soa_db.data_deletion import (
    assert_document_reference_policy_complete,
    delete_document_data,
)
from soa_db.data_export_jobs import DataExportJob, DataExportState, new_data_export_job
from soa_db.documents import (
    Document,
    DocumentRepository,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.evaluation_runs import EvaluationRun, EvaluationRunState
from soa_db.external_cleanup import (
    ExternalCleanupIntent,
    ExternalCleanupState,
    ExternalResourceType,
    stage_external_cleanup_intent,
)
from soa_db.gold_datasets import GoldDocument
from soa_db.jobs import Job, JobStatus, enqueue_job
from soa_db.outbox import OutboxEvent, OutboxStatus
from soa_db.repository import OrganizationContext
from soa_db.retention import DeletionState
from soa_db.runs import start_run
from soa_db.uploads import UploadSession, UploadSessionState
from soa_db.usage_ledger import UsageEntry, record_usage
from soa_storage import MemoryObjectStore, sha256_hex

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/complete-deletion.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


def test_every_typed_document_reference_has_an_explicit_policy() -> None:
    assert_document_reference_policy_complete(Base.metadata)


async def test_deletion_erases_copies_unlinks_evidence_and_anonymizes_shell(
    db: DatabaseSessions,
) -> None:
    store = MemoryObjectStore()
    stream_id = uuid.uuid4()
    document_id = uuid.uuid4()
    content = b"personal sales order data"
    content_hash = sha256_hex(content)
    artifact_key = f"orgs/{ORG}/documents/source/original.pdf"
    upload_key = f"orgs/{ORG}/documents/source/upload.tmp"
    part_key = f"data-exports/{ORG}/bundle/parts/source.json"
    manifest_key = f"data-exports/{ORG}/bundle/manifest.json"
    legacy_key = f"data-exports/{ORG}/{document_id}/legacy/records.json"
    for key in (artifact_key, upload_key, part_key, manifest_key, legacy_key):
        await store.put(key, content, content_type="application/json")

    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            document_id=document_id,
            stream_id=stream_id,
            source_channel=SourceChannel.EMAIL,
            original_filename="Alice-Sensitive-PO.pdf",
            content_sha256=content_hash,
            size_bytes=len(content),
            content_type="application/pdf",
            client_reference="customer-alice-42",
            source_metadata={"sender": "alice@example.test", "subject": "Private order"},
            actor_id="user:requester",
        )
        run = await start_run(
            session,
            CONTEXT,
            document_id=document.id,
            input_sha256=content_hash,
            stream_version_id=None,
            config_fingerprint="f" * 64,
            triggered_by="worker:test",
        )
        await create_artifact(
            session,
            CONTEXT,
            document_id=document.id,
            kind=ArtifactKind.ORIGINAL,
            object_key=artifact_key,
            sha256=content_hash,
            size_bytes=len(content),
            content_type="application/pdf",
        )
        await stage_external_cleanup_intent(
            session,
            organization_id=ORG,
            resource_type=ExternalResourceType.OBJECT,
            resource_locator=artifact_key,
            safe_error="test rollback compensation failed",
        )
        session.add(
            UploadSession(
                organization_id=ORG,
                stream_id=stream_id,
                document_id=document.id,
                state=UploadSessionState.COMPLETED.value,
                object_key=upload_key,
                declared_filename="Alice-Sensitive-PO.pdf",
                declared_content_type="application/pdf",
                declared_size_bytes=len(content),
                declared_sha256=content_hash,
                client_reference="customer-alice-42",
                expires_at=document.received_at,
                created_by="user:requester",
                completed_at=document.received_at,
            )
        )
        await record_usage(
            session,
            CONTEXT,
            provider="example",
            cost_category="extraction",
            estimated_cost_cents=17,
            actor_id="worker:test",
            document_id=document.id,
            run_id=run.id,
            source_reference=f"stage:{run.id}:extract",
        )
        dataset_version_id = uuid.uuid4()
        session.add(
            GoldDocument(
                organization_id=ORG,
                dataset_version_id=dataset_version_id,
                document_sha256=content_hash,
                source_document_id=document.id,
                split="test",
                expected_class="sales_order",
                ground_truth={"fields": {"customer": "Alice"}},
            )
        )
        evaluation = EvaluationRun(
            organization_id=ORG,
            stream_id=stream_id,
            candidate_fingerprint="f" * 64,
            dataset_version_id=dataset_version_id,
            state=EvaluationRunState.RUNNING.value,
            predictions={content_hash: {"fields": {"customer": "Alice"}}},
            checkpoint={"scores": {content_hash: {"fields_exact": 1}}},
            report={"documents_scored": 1},
            gate_result={"passed": True},
            attestation={
                "documents": {
                    content_hash: {
                        "source_document_id": str(document.id),
                        "document_sha256": content_hash,
                    }
                }
            },
            created_by="user:evaluator",
        )
        session.add(evaluation)
        await session.flush()
        export = new_data_export_job(
            organization_id=ORG,
            scope="document",
            document_id=document.id,
            created_by="user:auditor",
        )
        export.state = DataExportState.SUCCEEDED.value
        export.parts = [{"document_id": str(document.id), "object_key": part_key}]
        export.manifest_object_key = manifest_key
        session.add(export)
        other = await create_document(
            session,
            CONTEXT,
            stream_id=stream_id,
            source_channel=SourceChannel.UPLOAD,
            original_filename="duplicate.pdf",
            content_sha256="b" * 64,
            size_bytes=10,
            content_type="application/pdf",
            actor_id="user:test",
        )
        other.duplicate_of = document.id
        session.add(
            OutboxEvent(
                organization_id=ORG,
                event_type="document.registered",
                payload={
                    "document_id": str(document.id),
                    "content_sha256": content_hash,
                    "sender": "alice@example.test",
                },
                status=OutboxStatus.PENDING.value,
            )
        )
        await enqueue_job(
            session,
            job_type="document.preprocess",
            organization_id=ORG,
            payload={
                "organization_id": str(ORG),
                "document_id": str(document.id),
                "content_sha256": content_hash,
            },
        )
        await enqueue_job(
            session,
            job_type="data_export.cleanup",
            organization_id=ORG,
            payload={"organization_id": str(ORG), "object_keys": [legacy_key]},
        )
        await enqueue_job(
            session,
            job_type="evaluation.run",
            organization_id=ORG,
            payload={
                "organization_id": str(ORG),
                "evaluation_run_id": str(evaluation.id),
            },
        )
        await transition_document(
            session,
            CONTEXT,
            document=document,
            to_state=DocumentState.VALIDATING_FILE,
            actor_id="worker:test",
        )
        await transition_document(
            session,
            CONTEXT,
            document=document,
            to_state=DocumentState.QUARANTINED,
            actor_id="worker:test",
        )
        other_id = other.id

    async with db.session_scope() as session:
        result = await delete_document_data(
            session,
            store,
            CONTEXT,
            document_id=document_id,
            deletion_state=DeletionState.APPROVED,
            reason="verified data-subject erasure request",
            actor_id="user:approver",
        )

    assert result.object_keys_deleted == 5
    assert result.category_counts["upload_sessions"] == 1
    assert result.category_counts["usage_ledger_entries_anonymized"] == 1
    assert result.category_counts["gold_documents"] == 1
    assert result.category_counts["evaluation_runs_invalidated"] == 1
    assert result.category_counts["data_export_jobs_invalidated"] == 1
    assert result.category_counts["external_cleanup_intents_reconciled"] == 1
    assert not store._objects  # type: ignore[attr-defined]

    async with db.session_scope() as session:
        retained = await DocumentRepository(session, CONTEXT).get(document_id)
        assert retained is not None
        assert retained.state == DocumentState.DELETED.value
        assert retained.original_filename == "[deleted]"
        assert retained.content_sha256 == "0" * 64
        assert retained.size_bytes == 0
        assert retained.client_reference is None
        assert retained.source_metadata == {}
        assert retained.sla_due_at is None
        duplicate = await DocumentRepository(session, CONTEXT).get(other_id)
        assert duplicate is not None and duplicate.duplicate_of is None

        usage = (await session.execute(select(UsageEntry))).scalar_one()
        assert usage.document_id is None and usage.run_id is None
        assert usage.source_reference is None
        assert usage.estimated_cost_cents == 17, "financial evidence is preserved"
        assert (await session.execute(select(GoldDocument))).scalars().all() == []
        evaluation = (await session.execute(select(EvaluationRun))).scalar_one()
        assert evaluation.state == EvaluationRunState.CANCELLED.value
        assert evaluation.predictions == {}
        assert evaluation.checkpoint == {}
        assert evaluation.report is None
        assert evaluation.gate_result is None
        assert evaluation.attestation is None
        assert (await session.execute(select(UploadSession))).scalars().all() == []
        cleanup_intent = (await session.execute(select(ExternalCleanupIntent))).scalar_one()
        assert cleanup_intent.state == ExternalCleanupState.COMPLETED.value
        assert cleanup_intent.resource_locator is None
        assert cleanup_intent.last_error is None

        durable = (await session.execute(select(DataExportJob))).scalar_one()
        assert durable.state == DataExportState.EXPIRED.value
        assert durable.document_id is None and durable.cursor_document_id is None
        assert durable.parts == [] and durable.manifest_object_key is None

        outbox = (await session.execute(select(OutboxEvent))).scalar_one()
        assert outbox.status == OutboxStatus.FAILED.value
        assert outbox.payload == {
            "document_id": str(document_id),
            "redacted": "document data deleted",
        }
        document_job = (
            await session.execute(select(Job).where(Job.job_type == "document.preprocess"))
        ).scalar_one()
        assert document_job.status == JobStatus.CANCELLED.value
        assert document_job.payload["redacted"] == "document data deleted"
        cleanup_job = (
            await session.execute(select(Job).where(Job.job_type == "data_export.cleanup"))
        ).scalar_one()
        assert cleanup_job.status == JobStatus.CANCELLED.value
        assert cleanup_job.payload["redacted"] == "document data deleted"
        assert "object_keys" not in cleanup_job.payload
        evaluation_job = (
            await session.execute(select(Job).where(Job.job_type == "evaluation.run"))
        ).scalar_one()
        assert evaluation_job.status == JobStatus.CANCELLED.value
        assert evaluation_job.payload["redacted"] == "document data deleted"


def test_data_export_model_is_imported_for_reference_guard() -> None:
    # Keeps the import above intentional and makes the copied-bundle category
    # visible when reading this regression file.
    assert DataExportJob.__tablename__ == "data_export_jobs"
    assert Document.__tablename__ == "documents"
