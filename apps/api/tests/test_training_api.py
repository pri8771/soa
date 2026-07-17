"""Extraction-training API: training-set CRUD, annotation persistence, publish."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.domain.streams import StreamRepository
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import SourceChannel, create_document
from soa_db.repository import OrganizationContext

ADMIN = {"X-Dev-User": "user:admin"}


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/training-api.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    return TestClient(create_app(ApiSettings(environment=Environment.TEST), db=db)), db


async def _seed_stream(harness: tuple[TestClient, DatabaseSessions]) -> uuid.UUID:
    client, _ = harness
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "Orders", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "UK", "slug": "uk"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201, path
    return uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])


async def _make_document(
    db: DatabaseSessions, organization_id: uuid.UUID, *, filename: str, sha: str
) -> uuid.UUID:
    context = OrganizationContext(organization_id=organization_id)
    async with db.session_scope() as session:
        stream = await StreamRepository(session, context).get_by_slug("uk")
        assert stream is not None
        document = await create_document(
            session,
            context,
            stream_id=stream.id,
            source_channel=SourceChannel.UPLOAD,
            original_filename=filename,
            content_sha256=sha,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
        )
        return document.id


GROUND_TRUTH = {
    "fields": {"po_number": "PO-4711", "currency": "EUR"},
    "lines": [{"sku": "WIDGET-9", "quantity": "5"}],
    "validations": [],
    "regions": {
        "po_number": {"page_number": 1, "polygon": [[10, 10], [90, 10], [90, 30], [10, 30]]},
        "lines.0.sku": {"page_number": 1, "polygon": [[10, 40], [90, 40], [90, 60], [10, 60]]},
    },
}


async def test_training_set_crud_and_annotation_lifecycle(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    org_id = await _seed_stream(harness)
    doc_id = await _make_document(db, org_id, filename="sample.pdf", sha="a" * 64)

    base = "/orgs/northstar/streams/uk/training-sets"

    # Create a training set — a stream-scoped gold dataset with a draft v1.
    created = client.post(base, headers=ADMIN, json={"name": "UK samples", "slug": "uk-samples"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["stream_id"] is not None
    assert body["working_draft_version_id"] is not None
    assert body["published_version_id"] is None
    assert body["document_count"] == 0

    # Duplicate slug is refused.
    assert (
        client.post(base, headers=ADMIN, json={"name": "x", "slug": "uk-samples"}).status_code
        == 409
    )

    # It appears in the stream's list (and only for this stream).
    listing = client.get(base, headers=ADMIN)
    assert listing.status_code == 200
    assert [t["slug"] for t in listing.json()["items"]] == ["uk-samples"]

    # Save labels for the sample (draw + assign persisted as ground truth + regions).
    saved = client.put(
        f"{base}/uk-samples/documents",
        headers=ADMIN,
        json={"source_document_id": str(doc_id), "split": "train", "ground_truth": GROUND_TRUTH},
    )
    assert saved.status_code == 200, saved.text
    gold = saved.json()
    assert gold["document_sha256"] == "a" * 64
    assert gold["source_document_id"] == str(doc_id)
    assert gold["ground_truth"]["fields"]["po_number"] == "PO-4711"
    assert "po_number" in gold["ground_truth"]["regions"]

    # Re-saving the same sample replaces (idempotent upsert), not duplicates.
    resaved = client.put(
        f"{base}/uk-samples/documents",
        headers=ADMIN,
        json={
            "source_document_id": str(doc_id),
            "split": "validation",
            "ground_truth": {"fields": {"po_number": "PO-4711", "currency": "USD"}},
        },
    )
    assert resaved.status_code == 200, resaved.text
    docs = client.get(f"{base}/uk-samples/documents", headers=ADMIN).json()["items"]
    assert len(docs) == 1
    assert docs[0]["split"] == "validation"
    assert docs[0]["ground_truth"]["fields"]["currency"] == "USD"

    # Publish freezes the version.
    published = client.post(f"{base}/uk-samples/publish", headers=ADMIN)
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"

    # No draft remains: labelling is refused until a new draft opens.
    assert (
        client.put(
            f"{base}/uk-samples/documents",
            headers=ADMIN,
            json={
                "source_document_id": str(doc_id),
                "split": "train",
                "ground_truth": GROUND_TRUTH,
            },
        ).status_code
        == 409
    )

    # A new draft clones the published documents so iteration continues.
    new_draft = client.post(f"{base}/uk-samples/versions", headers=ADMIN)
    assert new_draft.status_code == 201, new_draft.text
    assert new_draft.json()["counts"]["total"] == 1

    detail = client.get(f"{base}/uk-samples", headers=ADMIN).json()
    assert detail["published_version_id"] is not None
    assert detail["working_draft_version_id"] is not None
    assert len(detail["versions"]) == 2


async def test_invalid_ground_truth_is_rejected(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    org_id = await _seed_stream(harness)
    doc_id = await _make_document(db, org_id, filename="s.pdf", sha="b" * 64)
    base = "/orgs/northstar/streams/uk/training-sets"
    client.post(base, headers=ADMIN, json={"name": "Set", "slug": "ts"})

    # Empty fields — the shape requires a non-empty fields object.
    bad_fields = client.put(
        f"{base}/ts/documents",
        headers=ADMIN,
        json={"source_document_id": str(doc_id), "split": "train", "ground_truth": {"fields": {}}},
    )
    assert bad_fields.status_code == 422

    # A malformed region polygon is refused.
    bad_region = client.put(
        f"{base}/ts/documents",
        headers=ADMIN,
        json={
            "source_document_id": str(doc_id),
            "split": "train",
            "ground_truth": {
                "fields": {"po_number": "X"},
                "regions": {"po_number": {"page_number": 1, "polygon": [[1, 2]]}},
            },
        },
    )
    assert bad_region.status_code == 422

    # An unknown split is refused.
    bad_split = client.put(
        f"{base}/ts/documents",
        headers=ADMIN,
        json={
            "source_document_id": str(doc_id),
            "split": "holdout",
            "ground_truth": {"fields": {"po_number": "X"}},
        },
    )
    assert bad_split.status_code == 422


async def test_publish_requires_documents_and_delete_rules(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    org_id = await _seed_stream(harness)
    doc_id = await _make_document(db, org_id, filename="s.pdf", sha="c" * 64)
    base = "/orgs/northstar/streams/uk/training-sets"
    client.post(base, headers=ADMIN, json={"name": "Set", "slug": "ts"})

    # An empty draft cannot publish.
    assert client.post(f"{base}/ts/publish", headers=ADMIN).status_code == 409

    # A draft-only set deletes cleanly.
    assert client.delete(f"{base}/ts", headers=ADMIN).status_code == 204
    assert client.get(f"{base}/ts", headers=ADMIN).status_code == 404

    # A set with a published version cannot be deleted (immutable evidence).
    client.post(base, headers=ADMIN, json={"name": "keep", "slug": "keep"})
    client.put(
        f"{base}/keep/documents",
        headers=ADMIN,
        json={
            "source_document_id": str(doc_id),
            "split": "train",
            "ground_truth": {"fields": {"po_number": "X"}},
        },
    )
    assert client.post(f"{base}/keep/publish", headers=ADMIN).status_code == 200
    assert client.delete(f"{base}/keep", headers=ADMIN).status_code == 409


async def test_sample_document_must_belong_to_stream(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    await _seed_stream(harness)
    base = "/orgs/northstar/streams/uk/training-sets"
    client.post(base, headers=ADMIN, json={"name": "Set", "slug": "ts"})
    missing = client.put(
        f"{base}/ts/documents",
        headers=ADMIN,
        json={
            "source_document_id": str(uuid.uuid4()),
            "split": "train",
            "ground_truth": {"fields": {"po_number": "X"}},
        },
    )
    assert missing.status_code == 404
