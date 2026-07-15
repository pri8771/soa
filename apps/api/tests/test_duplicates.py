"""Duplicate detection tests (ING-006): same file, same name/different
bytes, interleaved concurrent sessions, configurable policy — and never
a silent second delivery."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.domain.streams import Stream, StreamVersion
from soa_api.services.revalidation import revalidate_run
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.documents import Document
from soa_db.repository import OrganizationContext
from soa_storage import MemoryObjectStore, sha256_hex

ADMIN = {"X-Dev-User": "user:reviewer"}
PDF_A = b"%PDF-1.7 purchase order alpha"
PDF_B = b"%PDF-1.7 purchase order beta (different bytes)"


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions, MemoryObjectStore]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/duplicates.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    store = MemoryObjectStore()
    app = create_app(ApiSettings(environment=Environment.TEST), db=db, object_store=store)
    return TestClient(app, raise_server_exceptions=False), db, store


def seed_stream(client: TestClient) -> None:
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Email intake", "slug": "email"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201


async def set_duplicate_policy(db: DatabaseSessions, policy: str) -> None:
    async with db.session_scope() as session:
        stream = (await session.execute(select(Stream).where(Stream.slug == "email"))).scalar_one()
        version = StreamVersion(
            organization_id=stream.organization_id,
            stream_id=stream.id,
            version_number=1,
            state="published",
            overrides={"duplicate_policy": policy},
            resolved_snapshot={
                "process_version_id": str(uuid.uuid4()),
                "process_version_number": 1,
                "config": {"duplicate_policy": policy},
            },
            pinned_process_version_id=None,
        )
        session.add(version)
        await session.flush()
        stream.active_version_id = version.id


def open_session(client: TestClient, *, data: bytes, filename: str) -> dict[str, str]:
    response = client.post(
        "/orgs/northstar/streams/email/uploads",
        json={
            "filename": filename,
            "content_type": "application/pdf",
            "size_bytes": len(data),
            "sha256": sha256_hex(data),
        },
        headers=ADMIN,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def finish(
    client: TestClient, store: MemoryObjectStore, payload: dict[str, str], data: bytes
) -> dict[str, str]:
    await store.put(store.verify_signed_url(payload["upload_url"]), data)
    completed = client.post(
        f"/orgs/northstar/uploads/{payload['session_id']}/complete", headers=ADMIN
    )
    assert completed.status_code == 200, completed.text
    return dict(completed.json())


async def test_same_file_is_flagged_by_default_and_never_silent(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    seed_stream(client)
    first = await finish(client, store, open_session(client, data=PDF_A, filename="a.pdf"), PDF_A)
    second = await finish(client, store, open_session(client, data=PDF_A, filename="a.pdf"), PDF_A)
    # Default policy: flag — the duplicate proceeds but is marked.
    assert second["state"] == "queued"

    async with db.session_scope() as session:
        documents = {
            str(d.id): d for d in (await session.execute(select(Document))).scalars().all()
        }
        assert documents[second["document_id"]].duplicate_of == uuid.UUID(first["document_id"])
        assert documents[first["document_id"]].duplicate_of is None
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "document.duplicate_detected")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].summary["duplicate_of"] == first["document_id"]


async def test_name_changes_do_not_hide_duplicates_and_content_rules(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    seed_stream(client)
    await finish(client, store, open_session(client, data=PDF_A, filename="a.pdf"), PDF_A)
    # Same bytes under a different name: still a duplicate.
    renamed = await finish(
        client, store, open_session(client, data=PDF_A, filename="renamed.pdf"), PDF_A
    )
    # Different bytes under the same name: NOT a duplicate.
    different = await finish(
        client, store, open_session(client, data=PDF_B, filename="a.pdf"), PDF_B
    )

    async with db.session_scope() as session:
        documents = {
            str(d.id): d for d in (await session.execute(select(Document))).scalars().all()
        }
        assert documents[renamed["document_id"]].duplicate_of is not None
        assert documents[different["document_id"]].duplicate_of is None


async def test_reject_policy_refuses_the_second_delivery(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    seed_stream(client)
    await set_duplicate_policy(db, "reject")
    first = await finish(client, store, open_session(client, data=PDF_A, filename="a.pdf"), PDF_A)
    assert first["state"] == "queued"
    second = await finish(client, store, open_session(client, data=PDF_A, filename="a.pdf"), PDF_A)
    assert second["state"] == "rejected"

    async with db.session_scope() as session:
        rejected = (
            await session.execute(
                select(Document).where(Document.id == uuid.UUID(second["document_id"]))
            )
        ).scalar_one()
        assert first["document_id"] in (rejected.state_reason or "")
        assert rejected.duplicate_of == uuid.UUID(first["document_id"])


async def test_interleaved_concurrent_sessions_detect_each_other(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    """Both sessions open before either completes — the classic double
    submit. Whichever completes second must see the first."""
    client, db, store = harness
    seed_stream(client)
    session_one = open_session(client, data=PDF_A, filename="a.pdf")
    session_two = open_session(client, data=PDF_A, filename="a.pdf")

    first = await finish(client, store, session_one, PDF_A)
    second = await finish(client, store, session_two, PDF_A)
    assert first["state"] == "queued"
    assert second["state"] == "queued"  # default flag policy

    async with db.session_scope() as session:
        documents = {
            str(d.id): d for d in (await session.execute(select(Document))).scalars().all()
        }
        assert documents[second["document_id"]].duplicate_of == uuid.UUID(first["document_id"])


async def _validation_rule_keys(db: DatabaseSessions, document_id: str) -> set[str | None]:
    """The rule keys validation raises for the document (via the REV-009
    revalidation path, which shares the worker's duplicate gate)."""
    async with db.session_scope() as session:
        document = (
            await session.execute(select(Document).where(Document.id == uuid.UUID(document_id)))
        ).scalar_one()
        result = await revalidate_run(
            session,
            OrganizationContext(organization_id=document.organization_id),
            document=document,
            run_id=uuid.uuid4(),
        )
        return {reason.get("rule_key") for reason in result["decision"]["reasons"]}


async def test_allow_policy_is_processing_transparent_but_never_silent(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    seed_stream(client)
    await set_duplicate_policy(db, "allow")
    first = await finish(client, store, open_session(client, data=PDF_A, filename="a.pdf"), PDF_A)
    second = await finish(client, store, open_session(client, data=PDF_A, filename="a.pdf"), PDF_A)
    assert second["state"] == "queued"

    async with db.session_scope() as session:
        # Never silent: the marker and the audit event survive the policy.
        document = (
            await session.execute(
                select(Document).where(Document.id == uuid.UUID(second["document_id"]))
            )
        ).scalar_one()
        assert document.duplicate_of == uuid.UUID(first["document_id"])
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "document.duplicate_detected")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
    # Processing-transparent: validation raises no review-routing reason
    # from duplicates.business_hook under 'allow'.
    assert "duplicates.business_hook" not in await _validation_rule_keys(db, second["document_id"])


async def test_flag_policy_surfaces_the_duplicate_to_validation(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    seed_stream(client)  # no published version — the default policy (flag) applies
    await finish(client, store, open_session(client, data=PDF_A, filename="a.pdf"), PDF_A)
    second = await finish(client, store, open_session(client, data=PDF_A, filename="a.pdf"), PDF_A)
    assert "duplicates.business_hook" in await _validation_rule_keys(db, second["document_id"])
