"""Gold dataset tests (AIO-015): privacy classification with the
explicit-agreement gate, ground-truth shape validation, the version
lifecycle (drafts assemble, published freezes), and audit summaries
that never carry ground-truth values."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.gold_datasets import (
    GoldDataset,
    GoldDatasetError,
    GoldDatasetVersion,
    GoldDatasetVersionRepository,
    GoldDocumentRepository,
    PrivacyClassification,
    add_gold_document,
    create_dataset_version,
    create_gold_dataset,
    publish_dataset_version,
    published_dataset_version,
    validate_ground_truth,
)
from soa_db.repository import OrganizationContext
from soa_db.versioning import InvalidVersionStateError

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTEXT = OrganizationContext(organization_id=ORG)

GROUND_TRUTH: dict[str, Any] = {
    "fields": {"po_number": "PO-4711", "currency": "EUR", "ship_date": None},
    "lines": [{"sku": "WIDGET-9", "quantity": "5", "unit_price": "12.50"}],
    "validations": ["totals_match"],
}


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/gold.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def make_dataset_with_draft(db: DatabaseSessions) -> tuple[uuid.UUID, uuid.UUID]:
    async with db.session_scope() as session:
        dataset = await create_gold_dataset(
            session,
            CONTEXT,
            name="Pilot POs",
            slug="pilot-pos",
            privacy_classification=PrivacyClassification.CUSTOMER_CONFIDENTIAL,
            actor_id="user:u-1",
        )
        draft = await create_dataset_version(session, CONTEXT, dataset=dataset, actor_id="user:u-1")
        return dataset.id, draft.id


class TestPrivacyClassification:
    async def test_sharing_requires_the_recorded_agreement(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            with pytest.raises(GoldDatasetError, match="explicit agreement"):
                await create_gold_dataset(
                    session,
                    CONTEXT,
                    name="x",
                    slug="x",
                    privacy_classification=PrivacyClassification.CUSTOMER_SHARED_BY_AGREEMENT,
                    actor_id="user:u-1",
                )

    async def test_sharing_with_an_agreement_is_recorded(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            dataset = await create_gold_dataset(
                session,
                CONTEXT,
                name="Shared benchmark",
                slug="shared",
                privacy_classification=PrivacyClassification.CUSTOMER_SHARED_BY_AGREEMENT,
                sharing_agreement_ref="agreement:northstar-2026-07-01",
                actor_id="user:u-1",
            )
            assert dataset.sharing_agreement_ref == "agreement:northstar-2026-07-01"

    async def test_an_agreement_without_the_shared_classification_is_refused(
        self, db: DatabaseSessions
    ) -> None:
        async with db.session_scope() as session:
            with pytest.raises(GoldDatasetError, match="only makes sense"):
                await create_gold_dataset(
                    session,
                    CONTEXT,
                    name="x",
                    slug="x",
                    privacy_classification=PrivacyClassification.SYNTHETIC,
                    sharing_agreement_ref="agreement:whatever",
                    actor_id="user:u-1",
                )


class TestGroundTruthShape:
    def test_fields_or_lines_are_required(self) -> None:
        with pytest.raises(GoldDatasetError, match="at least one field or line item"):
            validate_ground_truth({"fields": {}, "lines": []})

    def test_line_items_alone_are_a_valid_sample(self) -> None:
        # A packing-list style sample labelled with only line items is valid.
        validate_ground_truth({"fields": {}, "lines": [{"sku": "A", "qty": "2"}]})

    def test_field_values_are_strings_or_null(self) -> None:
        with pytest.raises(GoldDatasetError, match="po_number"):
            validate_ground_truth({"fields": {"po_number": 4711}})

    def test_the_shape_is_closed(self) -> None:
        with pytest.raises(GoldDatasetError, match="closed"):
            validate_ground_truth({"fields": {"a": "b"}, "notes": "extra"})

    def test_a_valid_shape_passes(self) -> None:
        validate_ground_truth(GROUND_TRUTH)


class TestLifecycle:
    async def test_documents_join_drafts_with_split_and_class(self, db: DatabaseSessions) -> None:
        _, draft_id = await make_dataset_with_draft(db)
        async with db.session_scope() as session:
            draft = await GoldDatasetVersionRepository(session, CONTEXT).get(draft_id)
            assert draft is not None
            document = await add_gold_document(
                session,
                CONTEXT,
                version=draft,
                document_sha256="a" * 64,
                split="test",
                expected_class="purchase_order",
                ground_truth=GROUND_TRUTH,
                actor_id="user:u-1",
            )
            assert document.split == "test"
        async with db.session_scope() as session:
            docs = await GoldDocumentRepository(session, CONTEXT).list_for_version(draft_id)
            assert len(docs) == 1

    async def test_bad_splits_and_duplicate_documents_are_refused(
        self, db: DatabaseSessions
    ) -> None:
        _, draft_id = await make_dataset_with_draft(db)
        async with db.session_scope() as session:
            draft = await GoldDatasetVersionRepository(session, CONTEXT).get(draft_id)
            assert draft is not None
            with pytest.raises(GoldDatasetError, match="split"):
                await add_gold_document(
                    session,
                    CONTEXT,
                    version=draft,
                    document_sha256="a" * 64,
                    split="production",
                    ground_truth=GROUND_TRUTH,
                    actor_id="user:u-1",
                )
            await add_gold_document(
                session,
                CONTEXT,
                version=draft,
                document_sha256="a" * 64,
                split="test",
                ground_truth=GROUND_TRUTH,
                actor_id="user:u-1",
            )
            with pytest.raises(GoldDatasetError, match="already in this dataset version"):
                await add_gold_document(
                    session,
                    CONTEXT,
                    version=draft,
                    document_sha256="a" * 64,
                    split="test",
                    ground_truth=GROUND_TRUTH,
                    actor_id="user:u-1",
                )

    async def test_empty_versions_cannot_publish(self, db: DatabaseSessions) -> None:
        dataset_id, draft_id = await make_dataset_with_draft(db)
        async with db.session_scope() as session:
            dataset = await session.get(GoldDataset, dataset_id)
            draft = await session.get(GoldDatasetVersion, draft_id)
            assert dataset is not None and draft is not None
            with pytest.raises(GoldDatasetError, match="empty"):
                await publish_dataset_version(
                    session, CONTEXT, dataset=dataset, version=draft, actor_id="user:u-1"
                )

    async def test_publish_freezes_and_supersedes(self, db: DatabaseSessions) -> None:
        dataset_id, draft_id = await make_dataset_with_draft(db)
        async with db.session_scope() as session:
            dataset = await session.get(GoldDataset, dataset_id)
            draft = await session.get(GoldDatasetVersion, draft_id)
            assert dataset is not None and draft is not None
            await add_gold_document(
                session,
                CONTEXT,
                version=draft,
                document_sha256="a" * 64,
                split="test",
                ground_truth=GROUND_TRUTH,
                actor_id="user:u-1",
            )
            await publish_dataset_version(
                session, CONTEXT, dataset=dataset, version=draft, actor_id="user:u-1"
            )
            # Published versions accept no more documents.
            with pytest.raises(InvalidVersionStateError):
                await add_gold_document(
                    session,
                    CONTEXT,
                    version=draft,
                    document_sha256="b" * 64,
                    split="test",
                    ground_truth=GROUND_TRUTH,
                    actor_id="user:u-1",
                )
        async with db.session_scope() as session:
            dataset = await session.get(GoldDataset, dataset_id)
            assert dataset is not None
            second = await create_dataset_version(
                session, CONTEXT, dataset=dataset, actor_id="user:u-1"
            )
            await add_gold_document(
                session,
                CONTEXT,
                version=second,
                document_sha256="c" * 64,
                split="validation",
                ground_truth=GROUND_TRUTH,
                actor_id="user:u-1",
            )
            await publish_dataset_version(
                session, CONTEXT, dataset=dataset, version=second, actor_id="user:u-1"
            )
        async with db.session_scope() as session:
            live = await published_dataset_version(session, CONTEXT, dataset_id=dataset_id)
            assert live is not None and live.version_number == 2
            first = await session.get(GoldDatasetVersion, draft_id)
            assert first is not None and first.state == "superseded"


class TestAuditDiscipline:
    async def test_audit_carries_counts_never_ground_truth_values(
        self, db: DatabaseSessions
    ) -> None:
        dataset_id, draft_id = await make_dataset_with_draft(db)
        async with db.session_scope() as session:
            dataset = await session.get(GoldDataset, dataset_id)
            draft = await session.get(GoldDatasetVersion, draft_id)
            assert dataset is not None and draft is not None
            await add_gold_document(
                session,
                CONTEXT,
                version=draft,
                document_sha256="a" * 64,
                split="test",
                ground_truth=GROUND_TRUTH,
                actor_id="user:u-1",
            )
            await publish_dataset_version(
                session, CONTEXT, dataset=dataset, version=draft, actor_id="user:u-1"
            )
        async with db.session_scope() as session:
            events = (await session.execute(select(AuditEvent))).scalars().all()
            assert {e.action for e in events} >= {
                "gold_dataset.created",
                "gold_dataset.document_added",
                "gold_dataset.version_published",
            }
            for event in events:
                dumped = str(event.summary)
                assert "PO-4711" not in dumped
                assert "WIDGET-9" not in dumped


class TestRegionsAndUpsert:
    def test_regions_are_validated(self) -> None:
        # Valid: value maps plus optional positional hints.
        validate_ground_truth(
            {
                "fields": {"po_number": "PO-1"},
                "regions": {
                    "po_number": {
                        "page_number": 1,
                        "polygon": [[0, 0], [10, 0], [10, 5], [0, 5]],
                    }
                },
            }
        )
        with pytest.raises(GoldDatasetError, match="page_number"):
            validate_ground_truth(
                {"fields": {"a": "b"}, "regions": {"a": {"page_number": 0, "polygon": [[0, 0]]}}}
            )
        with pytest.raises(GoldDatasetError, match="polygon"):
            validate_ground_truth(
                {"fields": {"a": "b"}, "regions": {"a": {"page_number": 1, "polygon": [[0, 0]]}}}
            )
        with pytest.raises(GoldDatasetError, match="unknown keys"):
            validate_ground_truth(
                {
                    "fields": {"a": "b"},
                    "regions": {
                        "a": {"page_number": 1, "polygon": [[0, 0], [1, 0], [1, 1]], "z": 1}
                    },
                }
            )

    async def test_upsert_creates_then_replaces_in_a_draft(self, db: DatabaseSessions) -> None:
        from soa_db.gold_datasets import upsert_gold_document

        _, draft_id = await make_dataset_with_draft(db)
        async with db.session_scope() as session:
            draft = await GoldDatasetVersionRepository(session, CONTEXT).get(draft_id)
            assert draft is not None
            first = await upsert_gold_document(
                session,
                CONTEXT,
                version=draft,
                document_sha256="d" * 64,
                split="train",
                ground_truth={"fields": {"po_number": "PO-1"}},
                actor_id="user:u-1",
            )
            replaced = await upsert_gold_document(
                session,
                CONTEXT,
                version=draft,
                document_sha256="d" * 64,
                split="validation",
                ground_truth={"fields": {"po_number": "PO-2"}},
                actor_id="user:u-1",
            )
            assert first.id == replaced.id  # same row, replaced in place
            rows = await GoldDocumentRepository(session, CONTEXT).list_for_version(draft.id)
            assert len(rows) == 1
            assert rows[0].split == "validation"
            assert rows[0].ground_truth["fields"]["po_number"] == "PO-2"
