"""Duplicate detection tests (ING-006): same file, same name/different
bytes, interleaved concurrent sessions, configurable policy — and never
a silent second delivery."""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.domain.streams import StreamVersionRepository
from soa_api.services import duplicates as duplicate_service
from soa_api.services import ingestion as ingestion_service
from soa_api.services.revalidation import load_revalidation_config, revalidate_run
from soa_api.services.runtime_pins import resolve_runtime_pins
from soa_api.settings import ApiSettings, Environment
from soa_api.test_support.runtime_config import publish_runtime_config
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.documents import Document
from soa_db.repository import OrganizationContext
from soa_db.runs import start_run
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


async def seed_stream(
    client: TestClient, db: DatabaseSessions, *, duplicate_policy: str = "flag"
) -> uuid.UUID:
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Email intake", "slug": "email"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201
    return await publish_runtime_config(
        client, db, stream_overrides={"duplicate_policy": duplicate_policy}
    )


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
    await seed_stream(client, db)
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
    await seed_stream(client, db)
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
    await seed_stream(client, db, duplicate_policy="reject")
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
    await seed_stream(client, db)
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


async def test_exact_duplicate_fence_uses_tenant_stream_and_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advisory_lock = AsyncMock()
    monkeypatch.setattr(duplicate_service, "transaction_advisory_lock", advisory_lock)
    session = AsyncMock()
    organization_id = uuid.uuid4()
    stream_id = uuid.uuid4()
    content_sha256 = "a" * 64
    context = OrganizationContext(organization_id=organization_id)

    await duplicate_service.lock_exact_duplicate_intake(
        session,
        context,
        stream_id=stream_id,
        content_sha256=content_sha256,
    )

    advisory_lock.assert_awaited_once_with(
        session,
        "document-exact-duplicate-intake",
        organization_id,
        stream_id,
        content_sha256,
    )


async def test_shared_intake_wires_the_exact_duplicate_fence(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db, store = harness
    fence = AsyncMock()
    monkeypatch.setattr(ingestion_service, "lock_exact_duplicate_intake", fence)
    await seed_stream(client, db)

    completed = await finish(
        client,
        store,
        open_session(client, data=PDF_A, filename="fenced.pdf"),
        PDF_A,
    )

    fence.assert_awaited_once()
    _, fenced_context = fence.await_args.args
    async with db.session_scope() as session:
        document = (
            await session.execute(
                select(Document).where(Document.id == uuid.UUID(completed["document_id"]))
            )
        ).scalar_one()
    assert fenced_context.organization_id == document.organization_id
    assert fence.await_args.kwargs == {
        "stream_id": document.stream_id,
        "content_sha256": sha256_hex(PDF_A),
    }


async def _validation_rule_keys(
    db: DatabaseSessions,
    document_id: str,
    stream_version_id: uuid.UUID,
) -> set[str | None]:
    """The rule keys validation raises for the document (via the REV-009
    revalidation path, which shares the worker's duplicate gate)."""
    async with db.session_scope() as session:
        document = (
            await session.execute(select(Document).where(Document.id == uuid.UUID(document_id)))
        ).scalar_one()
        context = OrganizationContext(organization_id=document.organization_id)
        version = await StreamVersionRepository(session, context).get(stream_version_id)
        assert version is not None and isinstance(version.resolved_snapshot, dict)
        pins = await resolve_runtime_pins(
            session,
            context,
            stream_version_id=stream_version_id,
        )
        run = await start_run(
            session,
            context,
            document_id=document.id,
            input_sha256=document.content_sha256,
            stream_version_id=stream_version_id,
            config_fingerprint=pins.config_fingerprint,
            instruction_version_id=pins.instruction_version_id,
            confidence_policy_version_id=pins.confidence_policy_version_id,
            provider_policy_version_id=pins.provider_policy_version_id,
            provider_credential_ref=pins.provider_credential_ref,
            execution_fingerprint=pins.execution_fingerprint,
            triggered_by="test:duplicate-revalidation",
        )
        result = await revalidate_run(
            session,
            context,
            document=document,
            run_id=run.id,
        )
        return {reason.get("rule_key") for reason in result["decision"]["reasons"]}


async def test_allow_policy_is_processing_transparent_but_never_silent(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    stream_version_id = await seed_stream(client, db, duplicate_policy="allow")
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
    assert "duplicates.business_hook" not in await _validation_rule_keys(
        db, second["document_id"], stream_version_id
    )


async def test_revalidation_uses_the_runs_pinned_duplicate_policy(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    allow_version_id = await seed_stream(client, db, duplicate_policy="allow")
    await finish(client, store, open_session(client, data=PDF_A, filename="a.pdf"), PDF_A)
    duplicate = await finish(
        client,
        store,
        open_session(client, data=PDF_A, filename="again.pdf"),
        PDF_A,
    )

    # Publishing a newer active version must not alter revalidation of a run
    # already pinned to the earlier allow policy.
    await publish_runtime_config(client, db, stream_overrides={"duplicate_policy": "flag"})
    assert "duplicates.business_hook" not in await _validation_rule_keys(
        db,
        duplicate["document_id"],
        allow_version_id,
    )


async def test_revalidation_resolves_the_complete_pinned_contract(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    stream_version_id = await seed_stream(client, db, duplicate_policy="allow")
    uploaded = await finish(
        client,
        store,
        open_session(client, data=PDF_A, filename="contract.pdf"),
        PDF_A,
    )

    async with db.session_scope() as session:
        document = (
            await session.execute(
                select(Document).where(Document.id == uuid.UUID(uploaded["document_id"]))
            )
        ).scalar_one()
        context = OrganizationContext(organization_id=document.organization_id)
        pins = await resolve_runtime_pins(
            session,
            context,
            stream_version_id=stream_version_id,
        )
        run = await start_run(
            session,
            context,
            document_id=document.id,
            input_sha256=document.content_sha256,
            stream_version_id=stream_version_id,
            config_fingerprint=pins.config_fingerprint,
            instruction_version_id=pins.instruction_version_id,
            confidence_policy_version_id=pins.confidence_policy_version_id,
            provider_policy_version_id=pins.provider_policy_version_id,
            provider_credential_ref=pins.provider_credential_ref,
            execution_fingerprint=pins.execution_fingerprint,
            triggered_by="test:complete-revalidation-contract",
        )
        config = await load_revalidation_config(
            session,
            context,
            document=document,
            run_id=run.id,
        )

        assert [rule["key"] for rule in config.rules] == ["required.po_number"]
        assert config.field_types == {"po_number": "text"}
        assert config.normalizer_for("po_number") == "identifier"
        assert config.confidence_policy.standard_min_confidence == 0.86
        assert config.confidence_policy.field_min_confidence["po_number"] == 0.99
        assert config.stream_config["duplicate_policy"] == "allow"


async def test_flag_policy_surfaces_the_duplicate_to_validation(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    stream_version_id = await seed_stream(client, db)
    await finish(client, store, open_session(client, data=PDF_A, filename="a.pdf"), PDF_A)
    second = await finish(client, store, open_session(client, data=PDF_A, filename="a.pdf"), PDF_A)
    assert "duplicates.business_hook" in await _validation_rule_keys(
        db, second["document_id"], stream_version_id
    )
