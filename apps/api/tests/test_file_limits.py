"""File resource limit tests (ING-005): clamped stream configuration,
oversized/high-pixel/archive-bomb checks, audited violations."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.domain.streams import Stream, StreamVersion
from soa_api.services.file_limits import (
    FileLimits,
    LimitViolation,
    check_decompressed_size,
    check_page_count,
    check_pixels,
    check_size,
    resolve_limits,
)
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_storage import MemoryObjectStore

PLATFORM = FileLimits(
    max_size_bytes=50_000_000,
    max_pages=50,
    max_total_pixels=100_000_000,
    max_decompressed_bytes=250_000_000,
    max_conversion_seconds=120,
)
ADMIN = {"X-Dev-User": "user:reviewer"}


# --- policy resolution -------------------------------------------------------


def test_streams_can_lower_limits_but_never_raise_them() -> None:
    effective = resolve_limits(
        PLATFORM,
        {
            "max_upload_bytes": 1_000_000,  # lower: honored
            "max_pages": 500,  # higher: clamped to platform
            "max_total_pixels": "2000000",  # numeric string: honored
        },
    )
    assert effective.max_size_bytes == 1_000_000
    assert effective.max_pages == 50
    assert effective.max_total_pixels == 2_000_000
    # Untouched limits keep platform values.
    assert effective.max_decompressed_bytes == PLATFORM.max_decompressed_bytes


def test_malformed_or_nonpositive_config_never_changes_limits() -> None:
    effective = resolve_limits(
        PLATFORM, {"max_upload_bytes": "lots", "max_pages": -1, "max_conversion_seconds": None}
    )
    assert effective == PLATFORM


# --- checks ------------------------------------------------------------------


def test_oversized_high_pixel_and_bomb_checks() -> None:
    check_size(PLATFORM.max_size_bytes, PLATFORM)  # at the limit: fine
    with pytest.raises(LimitViolation, match="exceeds"):
        check_size(PLATFORM.max_size_bytes + 1, PLATFORM)
    with pytest.raises(LimitViolation, match="pages"):
        check_page_count(51, PLATFORM)
    # Image bomb: a tiny file declaring a colossal raster.
    with pytest.raises(LimitViolation, match="pixel"):
        check_pixels(30_000 * 30_000, PLATFORM)
    # Archive bomb: 1 KB compressed, 10 GB declared decompressed.
    with pytest.raises(LimitViolation, match="decompressed"):
        check_decompressed_size(10_000_000_000, PLATFORM)


# --- enforcement at upload declaration --------------------------------------


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/limits.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(
        ApiSettings(environment=Environment.TEST), db=db, object_store=MemoryObjectStore()
    )
    return TestClient(app, raise_server_exceptions=False), db


async def seed_stream_with_limit(client: TestClient, db: DatabaseSessions) -> None:
    """Stream whose published configuration lowers max_upload_bytes to 1000."""
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Email intake", "slug": "email"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201
    async with db.session_scope() as session:
        stream = (await session.execute(select(Stream).where(Stream.slug == "email"))).scalar_one()
        version = StreamVersion(
            organization_id=stream.organization_id,
            stream_id=stream.id,
            version_number=1,
            state="published",
            overrides={"max_upload_bytes": 1000},
            resolved_snapshot={
                "process_version_id": str(uuid.uuid4()),
                "process_version_number": 1,
                "config": {"max_upload_bytes": 1000},
            },
            pinned_process_version_id=None,
        )
        session.add(version)
        await session.flush()
        stream.active_version_id = version.id


async def test_stream_lowered_size_limit_is_enforced_and_audited(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    await seed_stream_with_limit(client, db)

    refused = client.post(
        "/orgs/northstar/streams/email/uploads",
        json={
            "filename": "big.pdf",
            "content_type": "application/pdf",
            "size_bytes": 2000,  # over the stream's 1000-byte limit
            "sha256": "a" * 64,
        },
        headers=ADMIN,
    )
    assert refused.status_code == 422
    assert "1000-byte limit" in refused.text

    # The violation is auditable even though the request failed.
    async with db.session_scope() as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "document.limit_violated")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].summary["limit"] == "max_size_bytes"

    # Within the stream's limit: accepted.
    accepted = client.post(
        "/orgs/northstar/streams/email/uploads",
        json={
            "filename": "small.pdf",
            "content_type": "application/pdf",
            "size_bytes": 900,
            "sha256": "a" * 64,
        },
        headers=ADMIN,
    )
    assert accepted.status_code == 201, accepted.text
