"""Cross-organization artifact verification (STO-005): the ops command's
core reconciles every tenant's records against the store and reports
corruption, loss, and orphans without exposing content."""

import json
import uuid
from pathlib import Path

import pytest

from soa_api.domain.tenancy import Organization
from soa_api.ops.verify_artifacts import verify_artifacts
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, create_artifact
from soa_db.repository import OrganizationContext
from soa_storage import MemoryObjectStore, sha256_hex


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/verify.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed_org_with_artifact(
    db: DatabaseSessions,
    store: MemoryObjectStore,
    *,
    slug: str,
    stored_bytes: bytes,
    recorded_bytes: bytes | None = None,
    write_object: bool = True,
) -> str:
    """Create an org + one artifact. ``recorded_bytes`` lets the DB record
    a different hash than the stored object (simulated corruption);
    ``write_object=False`` records an artifact whose object never landed."""
    async with db.session_scope() as session:
        organization = Organization(name=slug.title(), slug=slug)
        session.add(organization)
        await session.flush()
        org_id = organization.id
    key = f"orgs/{org_id}/documents/{uuid.uuid4()}/original/{uuid.uuid4().hex}-po.pdf"
    if write_object:
        await store.put(key, stored_bytes, content_type="application/pdf")
    async with db.session_scope() as session:
        await create_artifact(
            session,
            OrganizationContext(organization_id=org_id),
            document_id=uuid.uuid4(),
            kind=ArtifactKind.ORIGINAL,
            object_key=key,
            sha256=sha256_hex(recorded_bytes if recorded_bytes is not None else stored_bytes),
            size_bytes=len(stored_bytes),
            content_type="application/pdf",
        )
    return key


async def test_clean_multi_tenant_store_verifies(db: DatabaseSessions) -> None:
    store = MemoryObjectStore()
    await seed_org_with_artifact(db, store, slug="org-one", stored_bytes=b"one")
    await seed_org_with_artifact(db, store, slug="org-two", stored_bytes=b"two")
    report = await verify_artifacts(db, store)
    assert report.clean
    assert report.verified == 2


async def test_corruption_loss_and_orphans_across_tenants(db: DatabaseSessions) -> None:
    store = MemoryObjectStore()
    kept = await seed_org_with_artifact(db, store, slug="org-one", stored_bytes=b"intact")
    corrupt = await seed_org_with_artifact(
        db, store, slug="org-two", stored_bytes=b"EVIL BYTES", recorded_bytes=b"honest bytes"
    )
    lost = await seed_org_with_artifact(
        db, store, slug="org-three", stored_bytes=b"gone", write_object=False
    )
    await store.put("orgs/nobody/documents/x/original/orphan", b"unowned")

    report = await verify_artifacts(db, store)
    assert not report.clean
    assert report.verified == 1
    assert report.missing == [lost]
    assert report.extra == ["orgs/nobody/documents/x/original/orphan"]
    (mismatch,) = report.mismatched
    assert mismatch.key == corrupt
    assert kept not in report.missing

    serialized = json.dumps(report.to_dict())
    assert "EVIL BYTES" not in serialized and "unowned" not in serialized
