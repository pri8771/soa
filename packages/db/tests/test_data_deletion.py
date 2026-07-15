"""Data deletion workflow tests (SEC-010): complete deletion (objects
gone, derived rows erased, tombstone + audit written), the approval
gate, idempotent re-runs, and partial-failure recovery (a mid-run store
failure leaves the tombstone in progress and a retry completes it)."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, create_artifact
from soa_db.audit import AuditEvent
from soa_db.canonical_payloads import record_canonical_payload
from soa_db.catalog_selections import CatalogFieldSelection
from soa_db.data_deletion import (
    DeletionNotApprovedError,
    DeletionTombstone,
    delete_document_data,
)
from soa_db.documents import (
    DocumentRepository,
    SourceChannel,
    create_document,
)
from soa_db.extracted_fields import ExtractedField, create_extracted_field
from soa_db.repository import OrganizationContext
from soa_db.retention import DeletionState
from soa_db.runs import start_run
from soa_storage import MemoryObjectStore, ObjectNotFoundError, sha256_hex

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTEXT = OrganizationContext(organization_id=ORG)
STREAM = uuid.uuid4()


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/deletion.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed(db: DatabaseSessions, store: MemoryObjectStore) -> uuid.UUID:
    data = b"%PDF-1.7 deletable document bytes"
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=STREAM,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256=sha256_hex(data),
            size_bytes=len(data),
            content_type="application/pdf",
            actor_id="user:test",
        )
        run = await start_run(
            session,
            CONTEXT,
            document_id=document.id,
            input_sha256=sha256_hex(data),
            stream_version_id=None,
            config_fingerprint="f" * 64,
            triggered_by="user:test",
        )
        for index in range(3):
            key = f"orgs/{ORG}/docs/{document.id}/artifact-{index}"
            await store.put(key, data + str(index).encode(), content_type="application/pdf")
            await create_artifact(
                session,
                CONTEXT,
                document_id=document.id,
                kind=ArtifactKind.ORIGINAL if index == 0 else ArtifactKind.PAGE_IMAGE,
                object_key=key,
                sha256=sha256_hex(data + str(index).encode()),
                size_bytes=len(data) + 1,
                content_type="application/pdf",
            )
        await create_extracted_field(
            session,
            CONTEXT,
            document_id=document.id,
            run_id=run.id,
            field_key="po_number",
            raw_value="PO-100042",
            confidence=0.98,
            provider="mock",
        )
        session.add(
            CatalogFieldSelection(
                organization_id=ORG,
                document_id=document.id,
                run_id=run.id,
                task_id=None,
                field_key="lines.sku",
                row_index=0,
                status="selected",
                selection_source="machine",
                catalog_id=uuid.uuid4(),
                catalog_version_id=uuid.uuid4(),
                catalog_record_id=uuid.uuid4(),
                source_id="WID-100",
                display_name="Widget",
                matched_value="WID-100",
                value_fingerprint="f" * 64,
                decision_json=None,
                selected_by="worker",
            )
        )
        await record_canonical_payload(
            session,
            CONTEXT,
            document_id=document.id,
            run_id=run.id,
            task_id=None,
            schema_version="1.0.0",
            payload={"identifiers": {"po_number": "PO-100042"}},
            actor_id="user:test",
        )
        return document.id


async def test_complete_deletion_erases_objects_rows_and_leaves_a_tombstone(
    db: DatabaseSessions,
) -> None:
    store = MemoryObjectStore()
    document_id = await seed(db, store)

    async with db.session_scope() as session:
        result = await delete_document_data(
            session,
            store,
            CONTEXT,
            document_id=document_id,
            deletion_state=DeletionState.APPROVED,
            reason="customer erasure request",
            actor_id="user:admin",
        )

    assert result.object_keys_deleted == 3
    assert result.category_counts["artifacts"] == 3
    assert result.category_counts["extracted_fields"] == 1
    assert result.category_counts["canonical_payloads"] == 1
    assert result.category_counts["catalog_field_selections"] == 1
    assert result.category_counts["processing_runs"] == 1

    # Objects are gone from the store.
    assert len(store._objects) == 0  # type: ignore[attr-defined]

    async with db.session_scope() as session:
        # Derived rows erased...
        fields = (await session.execute(select(ExtractedField))).scalars().all()
        assert fields == []
        # ...the document row and the audit trail are KEPT (attributable).
        assert await DocumentRepository(session, CONTEXT).get(document_id) is not None
        tombstones = (await session.execute(select(DeletionTombstone))).scalars().all()
        assert len(tombstones) == 1
        assert tombstones[0].state == "completed"
        assert tombstones[0].completed_at is not None
        deleted_events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "document.data_deleted")
                )
            )
            .scalars()
            .all()
        )
        assert len(deleted_events) == 1
        # The audit summary carries counts, never the deleted content.
        assert "PO-100042" not in str(deleted_events[0].summary)


async def test_deletion_requires_approval(db: DatabaseSessions) -> None:
    store = MemoryObjectStore()
    document_id = await seed(db, store)
    for premature in (
        DeletionState.ELIGIBLE,
        DeletionState.PENDING_APPROVAL,
        DeletionState.RETAINED,
    ):
        async with db.session_scope() as session:
            with pytest.raises(DeletionNotApprovedError):
                await delete_document_data(
                    session,
                    store,
                    CONTEXT,
                    document_id=document_id,
                    deletion_state=premature,
                    reason="too early",
                    actor_id="user:admin",
                )
    # Nothing was deleted.
    assert len(store._objects) == 3  # type: ignore[attr-defined]


async def test_second_run_is_an_idempotent_no_op(db: DatabaseSessions) -> None:
    store = MemoryObjectStore()
    document_id = await seed(db, store)

    async def run() -> object:
        async with db.session_scope() as session:
            return await delete_document_data(
                session,
                store,
                CONTEXT,
                document_id=document_id,
                deletion_state=DeletionState.APPROVED,
                reason="erasure",
                actor_id="user:admin",
            )

    first = await run()
    second = await run()
    assert second.already_complete is True  # type: ignore[attr-defined]
    assert second.tombstone_id == first.tombstone_id  # type: ignore[attr-defined]
    async with db.session_scope() as session:
        # Still exactly one tombstone and one deletion audit event.
        assert len((await session.execute(select(DeletionTombstone))).scalars().all()) == 1
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "document.data_deleted")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1


class FlakyStore(MemoryObjectStore):
    """Deletes the first N keys, then raises — to model a mid-run
    failure. Clearing ``fail_after`` lets the retry complete."""

    def __init__(self, fail_after: int) -> None:
        super().__init__()
        self.fail_after = fail_after
        self.deletes = 0

    async def delete(self, key: str) -> None:
        if self.deletes >= self.fail_after:
            raise RuntimeError("simulated object-store outage")
        self.deletes += 1
        await super().delete(key)


async def test_partial_failure_leaves_progress_and_a_retry_completes(
    db: DatabaseSessions,
) -> None:
    store = FlakyStore(fail_after=2)
    document_id = await seed(db, store)

    # First attempt dies after deleting 2 of 3 objects.
    async with db.session_scope() as session:
        with pytest.raises(RuntimeError, match="outage"):
            await delete_document_data(
                session,
                store,
                CONTEXT,
                document_id=document_id,
                deletion_state=DeletionState.APPROVED,
                reason="erasure",
                actor_id="user:admin",
            )

    # Two objects gone; one survives; no completed tombstone yet, rows intact.
    assert len(store._objects) == 1  # type: ignore[attr-defined]
    async with db.session_scope() as session:
        assert (await session.execute(select(ExtractedField))).scalars().all() != []

    # The outage clears; a retry deletes what remains and completes.
    store.fail_after = 99
    async with db.session_scope() as session:
        result = await delete_document_data(
            session,
            store,
            CONTEXT,
            document_id=document_id,
            deletion_state=DeletionState.APPROVED,
            reason="erasure",
            actor_id="user:admin",
        )
    assert result.object_keys_deleted == 3
    assert len(store._objects) == 0  # type: ignore[attr-defined]
    async with db.session_scope() as session:
        assert (await session.execute(select(ExtractedField))).scalars().all() == []
        tombstones = (await session.execute(select(DeletionTombstone))).scalars().all()
        assert len(tombstones) == 1 and tombstones[0].state == "completed"


async def test_reconciliation_fails_loudly_if_an_object_survives(
    db: DatabaseSessions,
) -> None:
    class LyingStore(MemoryObjectStore):
        async def delete(self, key: str) -> None:
            return None  # claims success but keeps the object

    store = LyingStore()
    document_id = await seed(db, store)
    async with db.session_scope() as session:
        with pytest.raises(RuntimeError, match="did not reconcile"):
            await delete_document_data(
                session,
                store,
                CONTEXT,
                document_id=document_id,
                deletion_state=DeletionState.APPROVED,
                reason="erasure",
                actor_id="user:admin",
            )
    # Rows are NOT erased when reconciliation fails.
    async with db.session_scope() as session:
        assert (await session.execute(select(ExtractedField))).scalars().all() != []


def test_object_not_found_import() -> None:
    # The workflow relies on ObjectNotFoundError for idempotency; keep
    # the symbol available from the store package.
    assert issubclass(ObjectNotFoundError, Exception)
