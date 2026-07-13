"""Feature flag and quota tests (ANA-009): typed shapes with required
owner/review dates, the security-key refusal, audited changes, stream
overrides, and expiry inertness."""

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.feature_flags import FeatureFlagError, resolve_flag, set_flag
from soa_db.repository import OrganizationContext
from soa_db.types import utcnow

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("77777777-7777-4777-8777-777777777777")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/flags.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


def review() -> object:
    return utcnow() + timedelta(days=90)


class TestTypedShapes:
    async def test_flags_and_quotas_are_distinct_shapes(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            flag = await set_flag(
                session,
                CONTEXT,
                key="exports.csv_enabled",
                enabled=True,
                description="CSV export toggle",
                owner="user:owner",
                review_by=review(),
                actor_id="user:admin",
            )
            assert flag.kind == "flag"
            quota = await set_flag(
                session,
                CONTEXT,
                key="quota.monthly_cost_cents",
                limit_value=50_000,
                limit_unit="cents",
                description="Monthly spend budget",
                owner="user:finance",
                review_by=review(),
                actor_id="user:admin",
            )
            assert quota.kind == "quota"
            with pytest.raises(FeatureFlagError, match="not both"):
                await set_flag(
                    session,
                    CONTEXT,
                    key="broken.both",
                    enabled=True,
                    limit_value=1,
                    limit_unit="x",
                    description="d",
                    owner="o",
                    review_by=review(),
                    actor_id="user:admin",
                )
            with pytest.raises(FeatureFlagError, match="kinds never change"):
                await set_flag(
                    session,
                    CONTEXT,
                    key="exports.csv_enabled",
                    limit_value=1,
                    limit_unit="x",
                    description="d",
                    owner="o",
                    review_by=review(),
                    actor_id="user:admin",
                )

    async def test_owner_review_and_key_shape_are_required(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            with pytest.raises(FeatureFlagError, match="named owner"):
                await set_flag(
                    session,
                    CONTEXT,
                    key="a.flag",
                    enabled=True,
                    description="d",
                    owner="  ",
                    review_by=review(),
                    actor_id="user:admin",
                )
            with pytest.raises(FeatureFlagError, match="in the future"):
                await set_flag(
                    session,
                    CONTEXT,
                    key="a.flag",
                    enabled=True,
                    description="d",
                    owner="o",
                    review_by=utcnow() - timedelta(days=1),
                    actor_id="user:admin",
                )
            with pytest.raises(FeatureFlagError, match="lowercase dotted"):
                await set_flag(
                    session,
                    CONTEXT,
                    key="Not A Key",
                    enabled=True,
                    description="d",
                    owner="o",
                    review_by=review(),
                    actor_id="user:admin",
                )


class TestSecurityBoundary:
    async def test_security_keys_are_refused_outright(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            with pytest.raises(FeatureFlagError, match="not runtime toggles"):
                await set_flag(
                    session,
                    CONTEXT,
                    key="security.rls_enabled",
                    enabled=False,
                    description="nope",
                    owner="o",
                    review_by=review(),
                    actor_id="user:admin",
                )


class TestAuditAndResolution:
    async def test_changes_are_audited_with_before_and_after(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            await set_flag(
                session,
                CONTEXT,
                key="exports.csv_enabled",
                enabled=True,
                description="d",
                owner="user:owner",
                review_by=review(),
                actor_id="user:admin",
            )
            await set_flag(
                session,
                CONTEXT,
                key="exports.csv_enabled",
                enabled=False,
                description="d",
                owner="user:owner",
                review_by=review(),
                actor_id="user:admin",
            )
            events = (
                (
                    await session.execute(
                        select(AuditEvent).where(AuditEvent.action == "feature_flag.set")
                    )
                )
                .scalars()
                .all()
            )
            assert len(events) == 2
            assert events[0].summary["before"] is None
            assert events[1].summary["before"]["enabled"] is True
            assert events[1].summary["after"]["enabled"] is False

    async def test_stream_rows_override_and_expired_rows_are_inert(
        self, db: DatabaseSessions
    ) -> None:
        async with db.session_scope() as session:
            await set_flag(
                session,
                CONTEXT,
                key="quota.monthly_cost_cents",
                limit_value=10_000,
                limit_unit="cents",
                description="org budget",
                owner="o",
                review_by=review(),
                actor_id="user:admin",
            )
            await set_flag(
                session,
                CONTEXT,
                key="quota.monthly_cost_cents",
                limit_value=2_000,
                limit_unit="cents",
                description="stream budget",
                owner="o",
                review_by=review(),
                actor_id="user:admin",
                stream_id=STREAM,
            )
            stream_scoped = await resolve_flag(
                session, CONTEXT, key="quota.monthly_cost_cents", stream_id=STREAM
            )
            assert (stream_scoped.limit_value, stream_scoped.source) == (2_000, "stream")
            org_scoped = await resolve_flag(session, CONTEXT, key="quota.monthly_cost_cents")
            assert (org_scoped.limit_value, org_scoped.source) == (10_000, "organization")

            # Force the org row past its review date: it goes inert.
            from soa_db.feature_flags import FeatureFlagRepository

            row = await FeatureFlagRepository(session, CONTEXT).get_by_key(
                "quota.monthly_cost_cents", None
            )
            assert row is not None
            row.review_by = utcnow() - timedelta(days=1)
            await session.flush()
            expired = await resolve_flag(session, CONTEXT, key="quota.monthly_cost_cents")
            assert expired.limit_value is None
            assert expired.source == "expired"
            assert any("inert" in note for note in expired.notes)

    async def test_absent_flags_resolve_as_absent(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            resolved = await resolve_flag(session, CONTEXT, key="never.set")
            assert (resolved.source, resolved.enabled, resolved.limit_value) == (
                "absent",
                None,
                None,
            )
