"""Operator-initiated deletion API tests (SEC-010 surface): the
permission gate (data.delete), the active-pipeline refusal, complete
erasure (objects gone, derived rows gone, tombstone completed, document
row kept as deleted with the reason), idempotent repeat calls, and the
post-deletion protections (no reprocess, no cancel)."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, create_artifact
from soa_db.audit import AuditEvent
from soa_db.data_deletion import DeletionTombstone
from soa_db.documents import (
    DocumentRepository,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.extracted_fields import ExtractedField, create_extracted_field
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun, start_run
from soa_storage import MemoryObjectStore, sha256_hex

ADMIN = {"X-Dev-User": "user:admin"}  # org creator: org-admin (all permissions)
AUDITOR = {"X-Dev-User": "user:auditor"}  # has data.delete (like data.export)
REVIEWER = {"X-Dev-User": "user:reviewer"}  # NO data.delete
OUTSIDER = {"X-Dev-User": "user:supervisor"}  # org-admin of a DIFFERENT org (org B)

#: received -> ... -> the named state, along the happy chain.
_CHAIN = [
    DocumentState.VALIDATING_FILE,
    DocumentState.QUEUED,
    DocumentState.PREPROCESSING,
    DocumentState.CLASSIFYING,
    DocumentState.SPLITTING,
    DocumentState.EXTRACTING,
    DocumentState.NORMALIZING,
    DocumentState.VALIDATING_DATA,
]


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions, MemoryObjectStore]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/deletions.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    store = MemoryObjectStore()
    app = create_app(ApiSettings(environment=Environment.TEST), db=db, object_store=store)
    client = TestClient(app, raise_server_exceptions=False)
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Uploads", "slug": "uploads"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201
    for headers, email, role in (
        (AUDITOR, "auditor@northstar.example", "auditor"),
        (REVIEWER, "reviewer@northstar.example", "reviewer"),
    ):
        client.post("/orgs/northstar/invitations", json={"email": email}, headers=ADMIN)
        accepted = client.post(
            "/invitations/accept", json={"organization_slug": "northstar"}, headers=headers
        )
        assert (
            client.post(
                f"/orgs/northstar/members/{accepted.json()['membership_id']}/roles",
                json={"role_slug": role},
                headers=ADMIN,
            ).status_code
            == 201
        )
    return client, db, store


async def seed_document(
    client: TestClient,
    db: DatabaseSessions,
    store: MemoryObjectStore,
    *,
    to_state: DocumentState | None = DocumentState.FAILED_TERMINAL,
) -> str:
    """A document walked to ``to_state`` (None = leave at received) with
    stored objects, a run, and an extracted field to erase."""
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    stream_id = uuid.UUID(client.get("/orgs/northstar/streams", headers=ADMIN).json()[0]["id"])
    context = OrganizationContext(organization_id=org_id)
    data = b"%PDF-1.7 deletable document bytes"
    async with db.session_scope() as session:
        document = await create_document(
            session,
            context,
            stream_id=stream_id,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256=sha256_hex(data),
            size_bytes=len(data),
            content_type="application/pdf",
            actor_id="user:test",
        )
        if to_state is not None:
            tail = {
                DocumentState.APPROVED: [DocumentState.APPROVED],
                DocumentState.COMPLETED: [
                    DocumentState.APPROVED,
                    DocumentState.EXPORTING,
                    DocumentState.COMPLETED,
                ],
            }.get(to_state, [to_state])
            walk = _CHAIN if to_state not in _CHAIN else _CHAIN[: _CHAIN.index(to_state) + 1]
            steps = walk + tail if to_state not in _CHAIN else walk
            for state in steps:
                await transition_document(
                    session, context, document=document, to_state=state, actor_id="worker"
                )
        run = await start_run(
            session,
            context,
            document_id=document.id,
            input_sha256=sha256_hex(data),
            stream_version_id=None,
            config_fingerprint="f" * 64,
            triggered_by="user:test",
        )
        for index in range(2):
            key = f"orgs/{org_id}/docs/{document.id}/artifact-{index}"
            await store.put(key, data + str(index).encode(), content_type="application/pdf")
            await create_artifact(
                session,
                context,
                document_id=document.id,
                kind=ArtifactKind.ORIGINAL if index == 0 else ArtifactKind.PAGE_IMAGE,
                object_key=key,
                sha256=sha256_hex(data + str(index).encode()),
                size_bytes=len(data) + 1,
                content_type="application/pdf",
            )
        await create_extracted_field(
            session,
            context,
            document_id=document.id,
            run_id=run.id,
            field_key="po_number",
            raw_value="PO-100042",
            confidence=0.98,
            provider="mock",
        )
        return str(document.id)


def delete(client: TestClient, document_id: str, headers: dict[str, str], **body: object) -> object:
    payload = {"reason": "customer erasure request", **body}
    return client.post(
        f"/orgs/northstar/documents/{document_id}/deletion", json=payload, headers=headers
    )


# -- authorization ------------------------------------------------------------------


async def test_deletion_requires_the_data_delete_permission(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    document_id = await seed_document(client, db, store)
    forbidden = delete(client, document_id, REVIEWER)
    assert forbidden.status_code == 403
    # Nothing was deleted.
    assert len(store._objects) == 2  # type: ignore[attr-defined]


async def test_unknown_document_is_404(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, _db, _store = harness
    assert delete(client, str(uuid.uuid4()), AUDITOR).status_code == 404


# -- state matrix -------------------------------------------------------------------


@pytest.mark.parametrize("state", [None, DocumentState.QUEUED, DocumentState.EXTRACTING])
async def test_active_pipeline_states_are_refused(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
    state: DocumentState | None,
) -> None:
    client, db, store = harness
    document_id = await seed_document(client, db, store, to_state=state)
    response = delete(client, document_id, AUDITOR)
    assert response.status_code == 409
    assert "cancel processing first" in response.json()["error"]["message"]
    # Nothing was deleted.
    assert len(store._objects) == 2  # type: ignore[attr-defined]
    async with db.session_scope() as session:
        assert (await session.execute(select(DeletionTombstone))).scalars().all() == []


async def test_reason_is_required_and_bounded(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    document_id = await seed_document(client, db, store)
    assert delete(client, document_id, AUDITOR, reason="no").status_code == 422
    assert delete(client, document_id, AUDITOR, reason="x" * 501).status_code == 422


# -- happy path ---------------------------------------------------------------------


async def test_deletion_erases_data_and_settles_the_document_as_deleted(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    document_id = await seed_document(client, db, store)
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])

    response = delete(client, document_id, AUDITOR)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["document_id"] == document_id
    assert body["already_complete"] is False
    assert body["object_keys_deleted"] == 2
    assert body["category_counts"]["artifacts"] == 2
    assert body["category_counts"]["extracted_fields"] == 1
    assert body["category_counts"]["processing_runs"] == 1
    # No extracted VALUE leaks into the response envelope.
    assert "PO-100042" not in response.text

    # Objects are gone from the store; derived rows are erased.
    assert len(store._objects) == 0  # type: ignore[attr-defined]
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        assert (await session.execute(select(ExtractedField))).scalars().all() == []
        assert (await session.execute(select(ProcessingRun))).scalars().all() == []
        # The document row is KEPT, settled as deleted with the reason.
        document = await DocumentRepository(session, context).get(uuid.UUID(document_id))
        assert document is not None
        assert document.state == DocumentState.DELETED.value
        assert document.state_reason == "customer erasure request"
        # The retained tombstone is completed and the deletion is audited
        # counts-only by the workflow.
        (tombstone,) = (await session.execute(select(DeletionTombstone))).scalars().all()
        assert tombstone.state == "completed"
        assert str(tombstone.id) == body["tombstone_id"]
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
        assert "PO-100042" not in str(events[0].summary)


@pytest.mark.parametrize(
    "state",
    [
        DocumentState.REVIEW_REQUIRED,
        DocumentState.APPROVED,
        DocumentState.COMPLETED,
        DocumentState.CANCELLED,
    ],
)
async def test_every_settled_or_reviewable_state_is_deletable(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
    state: DocumentState,
) -> None:
    client, db, store = harness
    document_id = await seed_document(client, db, store, to_state=state)
    response = delete(client, document_id, AUDITOR)
    assert response.status_code == 201, response.text
    assert len(store._objects) == 0  # type: ignore[attr-defined]


async def test_repeat_deletion_is_idempotent(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    document_id = await seed_document(client, db, store)

    first = delete(client, document_id, AUDITOR)
    assert first.status_code == 201
    second = delete(client, document_id, AUDITOR, reason="repeat of the erasure request")
    assert second.status_code == 201, second.text
    body = second.json()
    assert body["already_complete"] is True
    assert body["tombstone_id"] == first.json()["tombstone_id"]
    assert body["object_keys_deleted"] == 2

    async with db.session_scope() as session:
        # Still one tombstone, one deletion event, and ONE state change to
        # deleted — the repeat call skipped the state write.
        assert len((await session.execute(select(DeletionTombstone))).scalars().all()) == 1
        events = (
            (await session.execute(select(AuditEvent).order_by(AuditEvent.occurred_at)))
            .scalars()
            .all()
        )
        assert len([e for e in events if e.action == "document.data_deleted"]) == 1
        to_deleted = [
            e
            for e in events
            if e.action == "document.state_changed" and (e.summary or {}).get("to") == "deleted"
        ]
        assert len(to_deleted) == 1


# -- post-deletion protections ---------------------------------------------------------


async def test_deleted_documents_cannot_be_reprocessed_or_cancelled(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    document_id = await seed_document(client, db, store)
    assert delete(client, document_id, AUDITOR).status_code == 201

    reprocess = client.post(
        f"/orgs/northstar/documents/{document_id}/reprocess",
        json={"reason": "try to resurrect it", "mode": "current_config"},
        headers=ADMIN,
    )
    assert reprocess.status_code == 409
    assert "deleted" in reprocess.json()["error"]["message"]

    cancel = client.post(
        f"/orgs/northstar/documents/{document_id}/cancel",
        json={"reason": "try to cancel it"},
        headers=ADMIN,
    )
    assert cancel.status_code == 409


# -- cross-tenant isolation (AGENTS.md §6) ---------------------------------------------


async def seed_other_org_document(
    client: TestClient, db: DatabaseSessions, store: MemoryObjectStore
) -> str:
    """A second organization ('southwind', org B) with one deletable
    document, created by an unrelated admin — used to prove an org-A caller
    cannot reach it."""
    for path, body in (
        ("/organizations", {"name": "Southwind", "slug": "southwind"}),
        ("/orgs/southwind/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/southwind/processes/purchase-orders/streams",
            {"name": "Uploads", "slug": "uploads"},
        ),
    ):
        assert client.post(path, json=body, headers=OUTSIDER).status_code == 201
    org_id = uuid.UUID(client.get("/orgs/southwind", headers=OUTSIDER).json()["id"])
    stream_id = uuid.UUID(client.get("/orgs/southwind/streams", headers=OUTSIDER).json()[0]["id"])
    context = OrganizationContext(organization_id=org_id)
    data = b"%PDF-1.7 org-B document bytes"
    async with db.session_scope() as session:
        document = await create_document(
            session,
            context,
            stream_id=stream_id,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256=sha256_hex(data),
            size_bytes=len(data),
            content_type="application/pdf",
            actor_id="user:test",
        )
        # received -> cancelled is a single valid transition into a
        # deletable state, so the document is genuinely erasable — the only
        # thing stopping deletion here is tenant scope, not its state.
        await transition_document(
            session,
            context,
            document=document,
            to_state=DocumentState.CANCELLED,
            actor_id="worker",
        )
        return str(document.id)


async def test_deletion_cannot_cross_tenants(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    """AUDITOR holds data.delete in org A (northstar), but the target
    document lives in org B (southwind). The tenant-scoped lookup makes it
    indistinguishable from a nonexistent document: the caller's OWN org
    path answers 404 — never 403 (which would confirm the id exists) or
    500. The org-B document must survive untouched."""
    client, db, store = harness
    foreign_id = await seed_other_org_document(client, db, store)

    response = delete(client, foreign_id, AUDITOR)
    assert response.status_code == 404

    # Org B's document is untouched: still present, still cancelled, and no
    # tombstone was ever minted for it.
    org_b = uuid.UUID(client.get("/orgs/southwind", headers=OUTSIDER).json()["id"])
    context = OrganizationContext(organization_id=org_b)
    async with db.session_scope() as session:
        document = await DocumentRepository(session, context).get(uuid.UUID(foreign_id))
        assert document is not None
        assert document.state == DocumentState.CANCELLED.value
        assert (await session.execute(select(DeletionTombstone))).scalars().all() == []


# -- rate limiting (SEC-003) ----------------------------------------------------------


async def test_deletion_is_capped_per_principal(tmp_path: Path) -> None:
    """The deletion endpoint is rate limited per principal, the same abuse
    control every other mutation carries. Like reprocess, the limiter runs
    BEFORE the document lookup, so a second probe answers 429 even though
    both would otherwise be 404 — abuse probing is throttled too."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/deletion-ratelimit.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(
        ApiSettings(environment=Environment.TEST, rate_limit_deletions_per_minute=1),
        db=DatabaseSessions(engine),
        object_store=MemoryObjectStore(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Uploads", "slug": "uploads"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201

    probe = "019f0000-0000-7000-8000-000000000000"
    assert delete(client, probe, ADMIN).status_code == 404
    limited = delete(client, probe, ADMIN)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert limited.headers["X-RateLimit-Remaining"] == "0"


# -- concurrency: StaleDataError -> 409 -----------------------------------------------


def test_stale_data_error_maps_to_409_conflict() -> None:
    """A concurrent modification during deletion surfaces as 409, not 500.

    Honest scope: this exercises the errors.py handler mapping directly,
    which is what the deletion endpoint (and every other read-then-flush on
    a VersionedMixin row — approvals, cancel, reprocess) relies on. A fully
    deterministic end-to-end race THROUGH the endpoint is NOT reproducible
    with the synchronous TestClient: the whole request runs as one blocking
    call, so a second session cannot bump the document's version in the
    window between the endpoint's load and its flush. What is proven here is
    exactly the contract the endpoint depends on — if the ORM flush loses
    the optimistic-concurrency race and raises StaleDataError, the client
    sees a 409 conflict with the safe envelope, never a 500 and never the
    raw SQLAlchemy message. It does NOT prove the endpoint itself flushes
    under a real concurrent writer; that remains an integration concern the
    sync harness cannot force."""
    from fastapi import FastAPI
    from sqlalchemy.orm.exc import StaleDataError

    from soa_api.errors import register_error_handlers

    app = FastAPI()
    register_error_handlers(app, ApiSettings(environment=Environment.TEST))

    @app.post("/boom")
    async def boom() -> None:
        raise StaleDataError("UPDATE documents ... matched 0 rows; concurrent writer won")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/boom")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "conflict"
    assert "concurrently" in body["error"]["message"]
    # The raw SQLAlchemy detail never reaches the client.
    assert "matched 0 rows" not in response.text
