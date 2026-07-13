"""Usage ledger tests (ANA-003): append-only immutability, idempotent
recording, billed units kept apart from estimated cost, adjustments as
audited signed deltas, and a reconcilable summary."""

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.repository import OrganizationContext
from soa_db.types import utcnow
from soa_db.usage_ledger import (
    UsageEntryImmutableError,
    UsageEntryRepository,
    UsageLedgerError,
    record_adjustment,
    record_usage,
    usage_summary,
)

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")
STREAM = uuid.UUID("77777777-7777-4777-8777-777777777777")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/ledger.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


class TestRecording:
    async def test_usage_records_billed_units_apart_from_the_estimate(
        self, db: DatabaseSessions
    ) -> None:
        async with db.session_scope() as session:
            entry = await record_usage(
                session,
                CONTEXT,
                provider="tesseract",
                provider_model=None,
                cost_category="ocr",
                estimated_cost_cents=12,
                billed_unit="pages",
                billed_quantity=3,
                stream_id=STREAM,
                page_count=3,
                actor_id="system:worker",
            )
            assert entry.billed_quantity == 3  # provider fact
            assert entry.estimated_cost_cents == 12  # our estimate
            assert entry.entry_type == "usage"

    async def test_source_reference_makes_recording_idempotent(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            first = await record_usage(
                session,
                CONTEXT,
                provider="mock",
                cost_category="extraction",
                estimated_cost_cents=5,
                source_reference="stage-run:abc:extract",
                actor_id="system:worker",
            )
            retried = await record_usage(
                session,
                CONTEXT,
                provider="mock",
                cost_category="extraction",
                estimated_cost_cents=5,
                source_reference="stage-run:abc:extract",
                actor_id="system:worker",
            )
            assert retried.id == first.id  # no double billing

    async def test_invalid_recordings_are_refused(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            with pytest.raises(UsageLedgerError, match="cost_category"):
                await record_usage(
                    session,
                    CONTEXT,
                    provider="mock",
                    cost_category="vibes",
                    estimated_cost_cents=1,
                    actor_id="system:worker",
                )
            with pytest.raises(UsageLedgerError, match="credits are adjustments"):
                await record_usage(
                    session,
                    CONTEXT,
                    provider="mock",
                    cost_category="ocr",
                    estimated_cost_cents=-1,
                    actor_id="system:worker",
                )
            with pytest.raises(UsageLedgerError, match="travel together"):
                await record_usage(
                    session,
                    CONTEXT,
                    provider="mock",
                    cost_category="ocr",
                    estimated_cost_cents=1,
                    billed_quantity=10,
                    actor_id="system:worker",
                )


class TestImmutability:
    async def test_entries_refuse_edits(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            entry = await record_usage(
                session,
                CONTEXT,
                provider="mock",
                cost_category="ocr",
                estimated_cost_cents=10,
                actor_id="system:worker",
            )
            entry_id = entry.id
        with pytest.raises(UsageEntryImmutableError):
            async with db.session_scope() as session:
                repo = UsageEntryRepository(session, CONTEXT)
                row = await repo.get(entry_id)
                assert row is not None
                row.estimated_cost_cents = 1
                await session.flush()

    async def test_adjustments_are_appended_signed_and_audited(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            entry = await record_usage(
                session,
                CONTEXT,
                provider="hosted-ocr",
                cost_category="ocr",
                estimated_cost_cents=100,
                billed_unit="pages",
                billed_quantity=10,
                actor_id="system:worker",
            )
            adjustment = await record_adjustment(
                session,
                CONTEXT,
                entry=entry,
                delta_cents=-25,
                reason="July invoice bills 10 pages at 7.5c, not the 10c estimate",
                actor_id="user:finance",
            )
            assert adjustment.entry_type == "adjustment"
            assert adjustment.adjustment_cents == -25
            assert adjustment.adjusts_entry_id == entry.id

            with pytest.raises(UsageLedgerError, match="written reason"):
                await record_adjustment(
                    session, CONTEXT, entry=entry, delta_cents=-1, reason="  ", actor_id="user:x"
                )
            with pytest.raises(UsageLedgerError, match="not to other adjustments"):
                await record_adjustment(
                    session,
                    CONTEXT,
                    entry=adjustment,
                    delta_cents=1,
                    reason="meta",
                    actor_id="user:x",
                )

            audited = (
                (
                    await session.execute(
                        select(AuditEvent).where(AuditEvent.action == "usage.adjusted")
                    )
                )
                .scalars()
                .all()
            )
            assert len(audited) == 1
            assert audited[0].summary["delta_cents"] == -25


class TestSummary:
    async def test_summary_groups_and_reconciles(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            ocr = await record_usage(
                session,
                CONTEXT,
                provider="hosted-ocr",
                cost_category="ocr",
                estimated_cost_cents=100,
                billed_unit="pages",
                billed_quantity=10,
                stream_id=STREAM,
                page_count=10,
                actor_id="system:worker",
            )
            await record_usage(
                session,
                CONTEXT,
                provider="local-llm",
                provider_model="qwen",
                cost_category="extraction",
                estimated_cost_cents=0,
                billed_unit="tokens",
                billed_quantity=52_000,
                stream_id=STREAM,
                actor_id="system:worker",
            )
            await record_adjustment(
                session,
                CONTEXT,
                entry=ocr,
                delta_cents=-25,
                reason="invoice reconciliation",
                actor_id="user:finance",
            )
            # Another tenant's usage stays invisible.
            await record_usage(
                session,
                OrganizationContext(organization_id=OTHER_ORG),
                provider="hosted-ocr",
                cost_category="ocr",
                estimated_cost_cents=999,
                actor_id="system:worker",
            )
            now = utcnow()
            summary = await usage_summary(
                session,
                CONTEXT,
                since=now - timedelta(hours=1),
                until=now + timedelta(hours=1),
            )
            by_key = {
                (group["provider"], group["cost_category"]): group for group in summary["groups"]
            }
            ocr_group = by_key[("hosted-ocr", "ocr")]
            assert ocr_group["estimated_cents"] == 100
            assert ocr_group["adjustment_cents"] == -25
            assert ocr_group["reconciled_cents"] == 75
            assert ocr_group["billed_quantity"] == 10
            assert ocr_group["billed_unit"] == "pages"
            llm_group = by_key[("local-llm", "extraction")]
            assert llm_group["billed_quantity"] == 52_000
            assert llm_group["provider_model"] == "qwen"
            assert summary["totals"] == {
                "estimated_cents": 100,
                "adjustment_cents": -25,
                "reconciled_cents": 75,
            }
            assert "facts" in summary["semantics"]["billed_quantity"]
