"""Classifier routing through the pipeline: an intake stream with a
published classifier re-routes a document to its target skill on the
document's own native text; ambiguity fails closed to a human; the routed
document completes a fresh run under the target's pins."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

# Config-plane models register their tables on the shared Base for this
# test's target-skill pins (the same test-only dependency test_run_config uses).
from soa_api.domain.policies import PolicyVersion
from soa_api.domain.streams import Stream, StreamVersion
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, create_artifact
from soa_db.audit import AuditEvent
from soa_db.classifiers import create_classifier_draft, publish_classifier_draft
from soa_db.documents import (
    DocumentRepository,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.instructions import InstructionVersion  # noqa: F401 — registers the table
from soa_db.jobs import Job, JobStatus
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRunRepository
from soa_storage.keys import artifact_key
from soa_storage.memory import MemoryObjectStore
from soa_storage.store import sha256_hex
from soa_worker.extraction.mock import MockExtractionProvider
from soa_worker.orchestrator import Orchestrator
from soa_worker.pipeline import build_executors
from soa_worker.run_config import snapshot_fingerprint

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
INTAKE = uuid.UUID("33333333-3333-4333-8333-333333333333")
TARGET = uuid.UUID("55555555-5555-4555-8555-555555555555")
TARGET_VERSION = uuid.UUID("66666666-6666-4666-8666-666666666666")
PROVIDER_POLICY = uuid.UUID("77777777-7777-4777-8777-777777777777")
CONTEXT = OrganizationContext(organization_id=ORG)

#: Real digital PDF whose native text contains "Acme GmbH" (AIO-002 corpus).
DIGITAL_PO = (Path(__file__).parent / "fixtures" / "pdfs" / "digital-po.pdf").read_bytes()
#: Real PDF with no extractable text — renders fine, can never match a route.
BLANK_PAGE = (Path(__file__).parent / "fixtures" / "pdfs" / "blank-page.pdf").read_bytes()


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/routing.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed_document(db: DatabaseSessions, store: MemoryObjectStore, data: bytes) -> uuid.UUID:
    sha = sha256_hex(data)
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=INTAKE,
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
        return document.id


async def seed_classifier(db: DatabaseSessions) -> None:
    """Published routing table on the intake + a routable target skill."""
    snapshot: dict[str, Any] = {
        "process_version_id": str(uuid.uuid4()),
        "process_version_number": 1,
        "config": {"provider_policy_version_id": str(PROVIDER_POLICY), "languages": ["en"]},
    }
    snapshot["fingerprint"] = snapshot_fingerprint(snapshot)
    async with db.session_scope() as session:
        session.add(
            Stream(
                id=TARGET,
                organization_id=ORG,
                process_id=uuid.uuid4(),
                name="Acme Skill",
                slug="acme-skill",
                status="active",
                active_version_id=TARGET_VERSION,
            )
        )
        session.add(
            StreamVersion(
                id=TARGET_VERSION,
                organization_id=ORG,
                stream_id=TARGET,
                version_number=1,
                overrides={},
                resolved_snapshot=snapshot,
                state="published",
            )
        )
        session.add(
            PolicyVersion(
                organization_id=ORG,
                id=PROVIDER_POLICY,
                policy_type="provider",
                version_number=1,
                definition={"provider_name": "mock", "capabilities": ["field_extraction"]},
                state="published",
            )
        )
        draft = await create_classifier_draft(
            session,
            CONTEXT,
            stream_id=INTAKE,
            content={
                "routes": [
                    {
                        "label": "acme",
                        "target_stream_id": str(TARGET),
                        "signals": ["Acme GmbH"],
                    }
                ]
            },
            actor_id="user:test",
        )
        await publish_classifier_draft(session, CONTEXT, draft=draft, actor_id="user:test")


def preprocess_payload(document_id: uuid.UUID) -> dict[str, Any]:
    return {
        "document_id": str(document_id),
        "organization_id": str(ORG),
        "stream_id": str(INTAKE),
        "stream_version_id": None,
        "config_fingerprint": None,
    }


async def pump(db: DatabaseSessions, orchestrator: Orchestrator, *, deliveries: int = 60) -> None:
    """Deliver pending stage AND preprocess jobs until the queue drains."""
    processed: set[uuid.UUID] = set()
    for _ in range(deliveries):
        async with db.session_scope() as session:
            jobs = (
                (
                    await session.execute(
                        select(Job).where(
                            Job.job_type.in_(["document.stage", "document.preprocess"]),
                            Job.status == JobStatus.PENDING.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            pending = [j for j in jobs if j.id not in processed]
            if not pending:
                return
            job = pending[0]
            processed.add(job.id)
            job_type = job.job_type
            payload = dict(job.payload)
        if job_type == "document.preprocess":
            await orchestrator.handle_preprocess(payload)
        else:
            await orchestrator.handle_stage(payload)


async def test_document_is_rerouted_to_the_matching_skill(db: DatabaseSessions) -> None:
    store = MemoryObjectStore()
    await seed_classifier(db)
    document_id = await seed_document(db, store, DIGITAL_PO)
    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        # Routed: the document now belongs to the target skill and completed
        # a fresh run there (mock pipeline runs to approved/review).
        assert document.stream_id == TARGET
        assert document.state in (
            DocumentState.APPROVED.value,
            DocumentState.REVIEW_REQUIRED.value,
        )

        runs = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        assert len(runs) == 2  # the routing run + the target-skill run
        routed_run = runs[-1]
        assert routed_run.stream_version_id == TARGET_VERSION
        assert routed_run.execution_fingerprint is not None

        events = (await session.execute(select(AuditEvent))).scalars().all()
        routing = [e for e in events if e.action == "document.routed"]
        assert len(routing) == 1
        assert routing[0].summary["to_stream_id"] == str(TARGET)
        assert routing[0].summary["label"] == "acme"
        assert "Acme GmbH" in routing[0].summary["matched_signals"]
        assert str(routing[0].summary["classifier_reference"]).startswith("classifier:")


async def test_unmatched_document_fails_closed_as_unrouted(db: DatabaseSessions) -> None:
    store = MemoryObjectStore()
    await seed_classifier(db)
    # A real PDF with no extractable text: preprocessing succeeds, but no
    # text can never match a route.
    document_id = await seed_document(db, store, BLANK_PAGE)
    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == DocumentState.FAILED_TERMINAL.value
        # The orchestrator prefixes the stage; the "unrouted:" marker is what
        # the manual routing endpoint queries on.
        assert document.state_reason is not None and "unrouted:" in document.state_reason


async def test_stream_without_classifier_is_untouched(db: DatabaseSessions) -> None:
    store = MemoryObjectStore()
    document_id = await seed_document(db, store, DIGITAL_PO)
    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.stream_id == INTAKE
        assert document.state in (
            DocumentState.APPROVED.value,
            DocumentState.REVIEW_REQUIRED.value,
        )
