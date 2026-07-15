import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table

from soa_db import GUID, DatabaseSessions, create_database_engine
from soa_db.organization_export import (
    ORGANIZATION_EXPORT_CATEGORIES,
    REDACTED,
    OrganizationExportCategory,
    collect_organization_export_page,
    sanitize_export_value,
)
from soa_db.types import utcnow

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG = uuid.UUID("99999999-9999-4999-8999-999999999999")


def category(key: str) -> OrganizationExportCategory:
    return next(item for item in ORGANIZATION_EXPORT_CATEGORIES if item.key == key)


@pytest.fixture
async def control_db(tmp_path: Path) -> tuple[DatabaseSessions, dict[str, Table]]:
    metadata = MetaData()
    tables = {
        "organizations": Table(
            "organizations",
            metadata,
            Column("id", GUID(), primary_key=True),
            Column("name", String(200), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        ),
        "workspaces": Table(
            "workspaces",
            metadata,
            Column("id", GUID(), primary_key=True),
            Column("organization_id", GUID(), nullable=False),
            Column("name", String(200), nullable=False),
            Column("configuration", JSON(), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        ),
        "users": Table(
            "users",
            metadata,
            Column("id", GUID(), primary_key=True),
            Column("email", String(320), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        ),
        "memberships": Table(
            "memberships",
            metadata,
            Column("id", GUID(), primary_key=True),
            Column("organization_id", GUID(), nullable=False),
            Column("user_id", GUID(), nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
        ),
        "integration_credentials": Table(
            "integration_credentials",
            metadata,
            Column("id", GUID(), primary_key=True),
            Column("organization_id", GUID(), nullable=False),
            Column("secret_reference", String(500), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        ),
    }
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/organization-export.db")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    return DatabaseSessions(engine), tables


async def test_allowlisted_page_is_bounded_tenant_scoped_and_snapshot_bounded(
    control_db: tuple[DatabaseSessions, dict[str, Table]],
) -> None:
    db, tables = control_db
    snapshot = utcnow()
    first_id = uuid.UUID("10000000-0000-4000-8000-000000000001")
    second_id = uuid.UUID("10000000-0000-4000-8000-000000000002")
    async with db.session_scope() as session:
        await session.execute(
            tables["workspaces"].insert(),
            [
                {
                    "id": first_id,
                    "organization_id": ORG,
                    "name": "first",
                    "configuration": {"access_token": "plaintext", "token_ref": "vault://one"},
                    "created_at": snapshot - timedelta(seconds=2),
                },
                {
                    "id": second_id,
                    "organization_id": ORG,
                    "name": "second",
                    "configuration": {},
                    "created_at": snapshot - timedelta(seconds=1),
                },
                {
                    "id": uuid.uuid4(),
                    "organization_id": OTHER_ORG,
                    "name": "foreign",
                    "configuration": {},
                    "created_at": snapshot - timedelta(seconds=1),
                },
                {
                    "id": uuid.uuid4(),
                    "organization_id": ORG,
                    "name": "future",
                    "configuration": {},
                    "created_at": snapshot + timedelta(seconds=1),
                },
            ],
        )

    async with db.session_scope() as session:
        first = await collect_organization_export_page(
            session,
            organization_id=ORG,
            snapshot_at=snapshot,
            category=category("workspaces"),
            limit=1,
        )
        assert first.complete is False
        assert [record["name"] for record in first.records] == ["first"]
        assert first.records[0]["configuration"] == {
            "access_token": REDACTED,
            "token_ref": "vault://one",
        }

        second = await collect_organization_export_page(
            session,
            organization_id=ORG,
            snapshot_at=snapshot,
            category=category("workspaces"),
            after_id=first.last_id,
            limit=1,
        )
        assert second.complete is True
        assert [record["name"] for record in second.records] == ["second"]


async def test_users_are_reached_only_through_target_memberships(
    control_db: tuple[DatabaseSessions, dict[str, Table]],
) -> None:
    db, tables = control_db
    snapshot = utcnow()
    target_user, foreign_user = uuid.uuid4(), uuid.uuid4()
    async with db.session_scope() as session:
        await session.execute(
            tables["users"].insert(),
            [
                {"id": target_user, "email": "member@example.com", "created_at": snapshot},
                {"id": foreign_user, "email": "foreign@example.com", "created_at": snapshot},
            ],
        )
        await session.execute(
            tables["memberships"].insert(),
            [
                {
                    "id": uuid.uuid4(),
                    "organization_id": ORG,
                    "user_id": target_user,
                    "created_at": snapshot,
                },
                {
                    "id": uuid.uuid4(),
                    "organization_id": OTHER_ORG,
                    "user_id": foreign_user,
                    "created_at": snapshot,
                },
            ],
        )

    async with db.session_scope() as session:
        page = await collect_organization_export_page(
            session,
            organization_id=ORG,
            snapshot_at=snapshot + timedelta(seconds=1),
            category=category("users"),
        )
        assert [record["email"] for record in page.records] == ["member@example.com"]


async def test_opaque_secret_references_remain_but_plaintext_secret_keys_are_redacted(
    control_db: tuple[DatabaseSessions, dict[str, Table]],
) -> None:
    db, tables = control_db
    snapshot = utcnow()
    async with db.session_scope() as session:
        await session.execute(
            tables["integration_credentials"].insert(),
            {
                "id": uuid.uuid4(),
                "organization_id": ORG,
                "secret_reference": "vault://tenant/integration/credential",
                "created_at": snapshot,
            },
        )
    async with db.session_scope() as session:
        page = await collect_organization_export_page(
            session,
            organization_id=ORG,
            snapshot_at=snapshot + timedelta(seconds=1),
            category=category("integration_credentials"),
        )
        assert page.records[0]["secret_reference"] == "vault://tenant/integration/credential"

    assert sanitize_export_value(
        {
            "password": "do-not-export",
            "key_hash": "one-way-digest",
            "credential_id": "opaque-id",
            "secret_kind": "webhook_hmac",
            "token_count": 42,
            "nested": {"refresh_token": "do-not-export"},
        }
    ) == {
        "password": REDACTED,
        "key_hash": "one-way-digest",
        "credential_id": "opaque-id",
        "secret_kind": "webhook_hmac",
        "token_count": 42,
        "nested": {"refresh_token": REDACTED},
    }


async def test_missing_allowlisted_table_and_unknown_category_fail_closed(
    control_db: tuple[DatabaseSessions, dict[str, Table]],
) -> None:
    db, _ = control_db
    async with db.session_scope() as session:
        with pytest.raises(ValueError, match="does not exist"):
            await collect_organization_export_page(
                session,
                organization_id=ORG,
                snapshot_at=utcnow(),
                category=category("roles"),
            )

        with pytest.raises(ValueError, match="not allowlisted"):
            await collect_organization_export_page(
                session,
                organization_id=ORG,
                snapshot_at=utcnow(),
                category=OrganizationExportCategory("unknown", "users", "unsafe"),
            )
