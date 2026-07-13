"""Documents queue API tests (ING-009): pagination, filters, projection,
and cursors that refuse to cross tenants or filter combinations."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import DocumentState, SourceChannel, create_document, transition_document
from soa_db.repository import OrganizationContext
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:reviewer"}
OUTSIDER = {"X-Dev-User": "user:supervisor"}


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/documents-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(
        ApiSettings(environment=Environment.TEST), db=db, object_store=MemoryObjectStore()
    )
    return TestClient(app, raise_server_exceptions=False), db


async def seed(client: TestClient, db: DatabaseSessions, count: int = 5) -> list[str]:
    """Org + stream + ``count`` documents (alternating filenames/channels;
    the last one moved to review_required)."""
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Email intake", "slug": "email"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    stream_id = uuid.UUID(client.get("/orgs/northstar/streams", headers=ADMIN).json()[0]["id"])
    context = OrganizationContext(organization_id=org_id)
    ids: list[str] = []
    async with db.session_scope() as session:
        for index in range(count):
            document = await create_document(
                session,
                context,
                stream_id=stream_id,
                source_channel=SourceChannel.UPLOAD if index % 2 == 0 else SourceChannel.API,
                original_filename=f"po-{index:03}.pdf" if index % 2 == 0 else f"inv-{index:03}.pdf",
                content_sha256=f"{index:x}" * 64 if index < 16 else "f" * 64,
                size_bytes=100 + index,
                content_type="application/pdf",
                priority=100 - index,
                actor_id="user:test",
            )
            ids.append(str(document.id))
        last = document
        await transition_document(
            session,
            context,
            document=last,
            to_state=DocumentState.VALIDATING_FILE,
            actor_id="worker",
        )
        await transition_document(
            session, context, document=last, to_state=DocumentState.QUEUED, actor_id="worker"
        )
    return ids


async def test_cursor_pagination_walks_newest_first_without_overlap(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    ids = await seed(client, db, count=5)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        query = f"limit=2{'&cursor=' + cursor if cursor else ''}"
        page = client.get(f"/orgs/northstar/documents?{query}", headers=ADMIN)
        assert page.status_code == 200, page.text
        body = page.json()
        seen.extend(item["id"] for item in body["items"])
        pages += 1
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
    assert pages == 3
    assert seen == list(reversed(ids)), "newest first, no overlap, no gaps"

    ascending = client.get("/orgs/northstar/documents?sort=received_asc", headers=ADMIN)
    assert [i["id"] for i in ascending.json()["items"]] == ids


async def test_filters_and_search(harness: tuple[TestClient, DatabaseSessions]) -> None:
    client, db = harness
    ids = await seed(client, db, count=5)

    queued = client.get("/orgs/northstar/documents?document_state=queued", headers=ADMIN).json()
    assert [i["id"] for i in queued["items"]] == [ids[-1]]

    api_channel = client.get("/orgs/northstar/documents?source_channel=api", headers=ADMIN).json()
    assert {i["source_channel"] for i in api_channel["items"]} == {"api"}
    assert len(api_channel["items"]) == 2

    named = client.get("/orgs/northstar/documents?search=po-000", headers=ADMIN).json()
    assert [i["original_filename"] for i in named["items"]] == ["po-000.pdf"]

    scoped = client.get("/orgs/northstar/documents?stream=email", headers=ADMIN).json()
    assert len(scoped["items"]) == 5
    missing = client.get("/orgs/northstar/documents?stream=nope", headers=ADMIN)
    assert missing.status_code == 404


async def test_field_projection(harness: tuple[TestClient, DatabaseSessions]) -> None:
    client, db = harness
    await seed(client, db, count=1)
    projected = client.get("/orgs/northstar/documents?fields=id,state", headers=ADMIN).json()
    assert set(projected["items"][0]) == {"id", "state"}
    unknown = client.get("/orgs/northstar/documents?fields=id,object_key", headers=ADMIN)
    assert unknown.status_code == 400
    assert "object_key" in unknown.text


async def test_cursors_cannot_cross_tenants_or_filters(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    await seed(client, db, count=3)

    page = client.get("/orgs/northstar/documents?limit=1", headers=ADMIN).json()
    cursor = page["next_cursor"]
    assert cursor is not None

    # Same filters: continues fine.
    ok = client.get(f"/orgs/northstar/documents?limit=1&cursor={cursor}", headers=ADMIN)
    assert ok.status_code == 200

    # Different filter combination: refused.
    crossed = client.get(
        f"/orgs/northstar/documents?limit=1&document_state=queued&cursor={cursor}",
        headers=ADMIN,
    )
    assert crossed.status_code == 400
    assert "different listing" in crossed.text

    # Another tenant: the same cursor is refused there too.
    assert (
        client.post(
            "/organizations", json={"name": "Other", "slug": "other-org"}, headers=OUTSIDER
        ).status_code
        == 201
    )
    foreign = client.get(f"/orgs/other-org/documents?limit=1&cursor={cursor}", headers=OUTSIDER)
    assert foreign.status_code == 400

    # Garbage cursors are 400, never 500.
    garbage = client.get("/orgs/northstar/documents?cursor=%%%", headers=ADMIN)
    assert garbage.status_code == 400

    # And the listing itself stays tenant-scoped.
    assert client.get("/orgs/northstar/documents", headers=OUTSIDER).status_code == 404
    empty = client.get("/orgs/other-org/documents", headers=OUTSIDER).json()
    assert empty["items"] == []


async def test_cancel_respects_the_state_machine(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    ids = await seed(client, db, count=3)  # ids[-1] ends up queued
    # ids[0] is still "received" (active): cancellable.
    cancelled = client.post(
        f"/orgs/northstar/documents/{ids[0]}/cancel",
        json={"reason": "uploaded to the wrong stream"},
        headers=ADMIN,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"

    # Re-cancelling is idempotent (same state, no new transition).
    again = client.post(
        f"/orgs/northstar/documents/{ids[0]}/cancel",
        json={"reason": "twice"},
        headers=ADMIN,
    )
    assert again.status_code == 200
    assert again.json()["state"] == "cancelled"

    # A settled (rejected) document refuses to cancel.
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        from soa_db.documents import DocumentRepository

        settled = await DocumentRepository(session, context).get(uuid.UUID(ids[1]))
        assert settled is not None
        await transition_document(
            session,
            context,
            document=settled,
            to_state=DocumentState.VALIDATING_FILE,
            actor_id="worker",
        )
        await transition_document(
            session,
            context,
            document=settled,
            to_state=DocumentState.REJECTED,
            reason="unsupported",
            actor_id="worker",
        )
    refused = client.post(
        f"/orgs/northstar/documents/{ids[1]}/cancel",
        json={"reason": "too late"},
        headers=ADMIN,
    )
    assert refused.status_code == 409

    # Cross-tenant invisibility.
    denied = client.post(
        f"/orgs/northstar/documents/{ids[1]}/cancel",
        json={"reason": "outsider"},
        headers=OUTSIDER,
    )
    assert denied.status_code == 404


async def test_detail_carries_artifacts_context_and_redacted_timeline(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    ids = await seed(client, db, count=1)
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    # Attach an original artifact so the timeline has artifact events too.
    from soa_db.artifacts import ArtifactKind, create_artifact

    async with db.session_scope() as session:
        await create_artifact(
            session,
            context,
            document_id=uuid.UUID(ids[0]),
            kind=ArtifactKind.ORIGINAL,
            object_key=f"orgs/{org_id}/documents/{ids[0]}/original/secret-key.pdf",
            sha256="0" * 64,
            size_bytes=100,
            content_type="application/pdf",
        )

    detail = client.get(f"/orgs/northstar/documents/{ids[0]}", headers=ADMIN)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["document"]["id"] == ids[0]
    assert body["context"]["stream_slug"] == "email"

    (artifact,) = body["artifacts"]
    assert artifact["kind"] == "original"
    # Sensitive internals never appear anywhere in the response.
    assert "object_key" not in detail.text
    assert "secret-key" not in detail.text

    actions = [entry["action"] for entry in body["timeline"]]
    assert actions == [
        "document.received",
        "document.state_changed",  # received -> validating_file
        "document.state_changed",  # validating_file -> queued
        "artifact.created",
    ], "occurrence order with stable tiebreak"
    assert all(entry["occurred_at"] for entry in body["timeline"])

    # Cross-tenant invisibility.
    assert client.get(f"/orgs/northstar/documents/{ids[0]}", headers=OUTSIDER).status_code == 404
