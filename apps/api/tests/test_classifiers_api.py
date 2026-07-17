"""Classifier API: routing-table lifecycle, target validation, the unrouted
queue, and audited manual routing."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.domain.streams import StreamRepository
from soa_api.settings import ApiSettings, Environment
from soa_api.test_support.runtime_config import publish_runtime_config
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import (
    DocumentRepository,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.jobs import Job
from soa_db.repository import OrganizationContext

ADMIN = {"X-Dev-User": "user:admin"}


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/classifiers-api.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    return TestClient(create_app(ApiSettings(environment=Environment.TEST), db=db)), db


async def _seed(harness: tuple[TestClient, DatabaseSessions]) -> tuple[uuid.UUID, str, str]:
    """Org + process + two streams: 'intake' and 'pharma' (the skill)."""
    client, _ = harness
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "Orders", "slug": "purchase-orders"}),
        ("/orgs/northstar/processes/purchase-orders/streams", {"name": "Intake", "slug": "intake"}),
        ("/orgs/northstar/processes/purchase-orders/streams", {"name": "Pharma", "slug": "pharma"}),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201, path
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    return org_id, "intake", "pharma"


async def _stream_id(db: DatabaseSessions, org_id: uuid.UUID, slug: str) -> uuid.UUID:
    async with db.session_scope() as session:
        stream = await StreamRepository(
            session, OrganizationContext(organization_id=org_id)
        ).get_by_slug(slug)
        assert stream is not None
        return stream.id


async def test_classifier_lifecycle_and_target_validation(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    org_id, intake, pharma = await _seed(harness)
    pharma_id = await _stream_id(db, org_id, pharma)

    # A route to a nonexistent stream is refused.
    bad = client.post(
        f"/orgs/northstar/streams/{intake}/classifier",
        headers=ADMIN,
        json={
            "content": {
                "routes": [
                    {
                        "label": "ghost",
                        "target_stream_id": str(uuid.uuid4()),
                        "signals": ["x"],
                    }
                ]
            }
        },
    )
    assert bad.status_code == 422

    created = client.post(
        f"/orgs/northstar/streams/{intake}/classifier",
        headers=ADMIN,
        json={
            "content": {
                "routes": [
                    {
                        "label": "pharma",
                        "target_stream_id": str(pharma_id),
                        "signals": ["Mawdsley", "Phoenix Healthcare"],
                    }
                ]
            }
        },
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["id"]
    assert created.json()["state"] == "draft"

    published = client.post(
        f"/orgs/northstar/classifier-versions/{version_id}/publish", headers=ADMIN
    )
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"
    assert published.json()["reference"].startswith("classifier:")

    # Published versions are immutable; a new draft numbers v2.
    stale = client.patch(
        f"/orgs/northstar/classifier-versions/{version_id}",
        headers=ADMIN,
        json={
            "content": {
                "routes": [{"label": "x", "target_stream_id": str(pharma_id), "signals": ["y"]}]
            }
        },
    )
    assert stale.status_code == 409

    listing = client.get(f"/orgs/northstar/streams/{intake}/classifier", headers=ADMIN)
    assert [v["version_number"] for v in listing.json()["items"]] == [1]


async def test_unrouted_queue_and_manual_route(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    org_id, intake, pharma = await _seed(harness)
    # The manual route re-pins against the TARGET's published config.
    await publish_runtime_config(
        client, db, stream_slug=pharma, process_slug="purchase-orders", headers=ADMIN
    )
    context = OrganizationContext(organization_id=org_id)
    intake_id = await _stream_id(db, org_id, intake)

    async with db.session_scope() as session:
        document = await create_document(
            session,
            context,
            stream_id=intake_id,
            source_channel=SourceChannel.UPLOAD,
            original_filename="mystery.pdf",
            content_sha256="a" * 64,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
        )
        document_id = document.id
        for state, reason in (
            (DocumentState.VALIDATING_FILE, None),
            (DocumentState.QUEUED, None),
            (DocumentState.PREPROCESSING, None),
            (DocumentState.CLASSIFYING, None),
            (
                DocumentState.FAILED_TERMINAL,
                "stage classifying failed: unrouted: no classifier route matched",
            ),
        ):
            await transition_document(
                session,
                context,
                document=document,
                to_state=state,
                reason=reason,
                actor_id="worker",
            )

    unrouted = client.get("/orgs/northstar/routing/unrouted", headers=ADMIN)
    assert unrouted.status_code == 200
    items = unrouted.json()["items"]
    assert [item["id"] for item in items] == [str(document_id)]

    routed = client.post(
        f"/orgs/northstar/documents/{document_id}/route",
        headers=ADMIN,
        json={"stream_slug": pharma},
    )
    assert routed.status_code == 200, routed.text
    assert routed.json()["state"] == "queued"

    async with db.session_scope() as session:
        refreshed = await DocumentRepository(session, context).get(document_id)
        assert refreshed is not None
        assert refreshed.state == DocumentState.QUEUED.value
        stream = await StreamRepository(session, context).get(refreshed.stream_id)
        assert stream is not None and stream.slug == pharma
        jobs = (
            (await session.execute(select(Job).where(Job.job_type == "document.preprocess")))
            .scalars()
            .all()
        )
        routed_jobs = [j for j in jobs if "manual-route" in (j.dedupe_key or "")]
        assert len(routed_jobs) == 1
        assert routed_jobs[0].payload["stream_id"] == str(refreshed.stream_id)
        assert routed_jobs[0].payload.get("execution_fingerprint")

    # A queued document is no longer unrouted; routing again is refused.
    again = client.post(
        f"/orgs/northstar/documents/{document_id}/route",
        headers=ADMIN,
        json={"stream_slug": pharma},
    )
    assert again.status_code == 409
