"""DB-backed job runner (wires the pipeline into the claim loop).

Proves the same PRC-012 synthetic order reaches ``approved`` when driven
through the real jobs table — claim (JOB-003) -> orchestrator handler ->
mark succeeded (JOB-005) -> the handler's enqueued next-stage job claimed
on the following poll — rather than the in-test manual pump.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, create_artifact
from soa_db.documents import (
    DocumentRepository,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.jobs import Job, JobStatus, enqueue_job
from soa_db.repository import OrganizationContext
from soa_storage.keys import artifact_key
from soa_storage.memory import MemoryObjectStore
from soa_storage.store import sha256_hex
from soa_worker.database_queue import DatabaseJobQueue
from soa_worker.extraction.mock import SYNTHETIC_SALES_ORDER, MockExtractionProvider
from soa_worker.orchestrator import STAGE_JOB_TYPE, Orchestrator
from soa_worker.pipeline import build_executors
from soa_worker.registry import HandlerRegistry, JobEnvelope

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("33333333-3333-4333-8333-333333333333")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/runner.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def _seed_queued_document(db: DatabaseSessions, store: MemoryObjectStore) -> uuid.UUID:
    data = SYNTHETIC_SALES_ORDER
    sha = sha256_hex(data)
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=STREAM,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256=sha,
            size_bytes=len(data),
            content_type="application/pdf",
            actor_id="user:test",
        )
        key = artifact_key(ORG, document.id, kind="original", filename="po.pdf")
        await store.put(key, data, content_type="application/pdf")
        await create_artifact(
            session,
            CONTEXT,
            document_id=document.id,
            kind=ArtifactKind.ORIGINAL,
            object_key=key,
            sha256=sha,
            size_bytes=len(data),
            content_type="application/pdf",
        )
        for state in (DocumentState.VALIDATING_FILE, DocumentState.QUEUED):
            await transition_document(
                session, CONTEXT, document=document, to_state=state, actor_id="worker"
            )
        # What intake (ING-007) enqueues when a document reaches queued.
        await enqueue_job(
            session,
            job_type="document.preprocess",
            payload={
                "document_id": str(document.id),
                "organization_id": str(ORG),
                "stream_id": str(STREAM),
                "stream_version_id": None,
                "config_fingerprint": "f" * 64,
            },
            organization_id=ORG,
            dedupe_key=f"document.preprocess:{document.id}",
        )
        return document.id


async def test_claim_loop_drives_document_to_approved(db: DatabaseSessions) -> None:
    store = MemoryObjectStore()
    document_id = await _seed_queued_document(db, store)
    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    registry = HandlerRegistry()

    @registry.register("document.preprocess")
    async def preprocess(job: JobEnvelope) -> None:
        await orchestrator.handle_preprocess(job.payload)

    @registry.register(STAGE_JOB_TYPE)
    async def stage(job: JobEnvelope) -> None:
        await orchestrator.handle_stage(job.payload)

    queue = DatabaseJobQueue(db, worker_id="worker:vertical-slice")

    # Drive the worker loop by hand: claim one job, dispatch it, repeat.
    dispatched = 0
    for _ in range(100):
        envelope = await queue.claim()
        if envelope is None:
            break
        try:
            await registry.resolve(envelope.job_type)(envelope)
        except Exception as error:
            await queue.failed(envelope, error)
            raise
        else:
            await queue.succeeded(envelope)
        dispatched += 1

    assert dispatched >= len(("preprocess", "stages...")), "did real work"
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "approved"
        # Every job the pipeline created was claimed and completed.
        jobs = (await session.execute(select(Job))).scalars().all()
        assert jobs, "jobs were enqueued"
        assert all(j.status == JobStatus.SUCCEEDED.value for j in jobs), [
            (j.job_type, j.status) for j in jobs
        ]
