"""Immutable per-run configuration verification."""

import uuid
from pathlib import Path

import pytest

from soa_api.domain.streams import StreamVersion
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun
from soa_worker.run_config import RunConfigError, snapshot_fingerprint, verify_run_config

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")
VERSION = uuid.UUID("33333333-3333-4333-8333-333333333333")


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/config.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


def _snapshot() -> dict[str, object]:
    value: dict[str, object] = {
        "process_version_id": str(uuid.uuid4()),
        "process_version_number": 4,
        "config": {"languages": ["en"]},
    }
    value["fingerprint"] = snapshot_fingerprint(value)
    return value


def _run(fingerprint: str) -> ProcessingRun:
    return ProcessingRun(
        organization_id=ORG,
        document_id=uuid.uuid4(),
        run_number=1,
        stream_version_id=VERSION,
        config_fingerprint=fingerprint,
        input_sha256="a" * 64,
        triggered_by="test",
    )


async def _seed(db: DatabaseSessions, snapshot: dict[str, object]) -> None:
    async with db.session_scope() as session:
        session.add(
            StreamVersion(
                id=VERSION,
                organization_id=ORG,
                stream_id=uuid.uuid4(),
                version_number=1,
                overrides={},
                resolved_snapshot=snapshot,
                state="published",
            )
        )


async def test_verifies_exact_tenant_version_and_fingerprint(db: DatabaseSessions) -> None:
    snapshot = _snapshot()
    await _seed(db, snapshot)
    async with db.session_scope() as session:
        resolved = await verify_run_config(
            session,
            OrganizationContext(organization_id=ORG),
            _run(str(snapshot["fingerprint"])),
        )
    assert resolved == snapshot


async def test_rejects_run_fingerprint_mismatch(db: DatabaseSessions) -> None:
    await _seed(db, _snapshot())
    async with db.session_scope() as session:
        with pytest.raises(RunConfigError, match="does not match"):
            await verify_run_config(
                session,
                OrganizationContext(organization_id=ORG),
                _run("f" * 64),
            )


async def test_rejects_cross_tenant_lookup(db: DatabaseSessions) -> None:
    snapshot = _snapshot()
    await _seed(db, snapshot)
    async with db.session_scope() as session:
        with pytest.raises(RunConfigError, match="does not exist for this tenant"):
            await verify_run_config(
                session,
                OrganizationContext(organization_id=OTHER_ORG),
                _run(str(snapshot["fingerprint"])),
            )
