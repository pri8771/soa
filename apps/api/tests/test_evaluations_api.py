"""Evaluation API modes and immutable server execution contracts."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.domain.streams import StreamRepository
from soa_api.settings import ApiSettings, Environment
from soa_api.test_support.runtime_config import publish_runtime_config
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, create_artifact
from soa_db.documents import SourceChannel, create_document
from soa_db.evaluation_runs import EvaluationRunRepository
from soa_db.gold_datasets import (
    PrivacyClassification,
    add_gold_document,
    create_dataset_version,
    create_gold_dataset,
    publish_dataset_version,
)
from soa_db.repository import OrganizationContext

ADMIN = {"X-Dev-User": "user:reviewer"}


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/evaluations-api.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    return TestClient(create_app(ApiSettings(environment=Environment.TEST), db=db)), db


async def _seed(harness: tuple[TestClient, DatabaseSessions]) -> tuple[uuid.UUID, uuid.UUID]:
    client, db = harness
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "Orders", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Email", "slug": "email"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201
    await publish_runtime_config(client, db)
    draft = client.post(
        "/orgs/northstar/streams/email/versions",
        headers=ADMIN,
        json={"overrides": {"languages": ["en"], "confidence_floor": 0.91}},
    )
    assert draft.status_code == 201, draft.text
    stream_version_id = uuid.UUID(draft.json()["id"])
    organization_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=organization_id)
    sha = "a" * 64
    async with db.session_scope() as session:
        stream = await StreamRepository(session, context).get_by_slug("email")
        assert stream is not None
        source = await create_document(
            session,
            context,
            stream_id=stream.id,
            source_channel=SourceChannel.UPLOAD,
            original_filename="benchmark.pdf",
            content_sha256=sha,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
        )
        await create_artifact(
            session,
            context,
            document_id=source.id,
            kind=ArtifactKind.ORIGINAL,
            object_key=f"orgs/{organization_id}/documents/{source.id}/original/benchmark.pdf",
            sha256=sha,
            size_bytes=100,
            content_type="application/pdf",
        )
        dataset = await create_gold_dataset(
            session,
            context,
            name="Production benchmark",
            slug="production-benchmark",
            privacy_classification=PrivacyClassification.SYNTHETIC,
            actor_id="user:test",
        )
        version = await create_dataset_version(
            session, context, dataset=dataset, actor_id="user:test"
        )
        await add_gold_document(
            session,
            context,
            version=version,
            document_sha256=sha,
            source_document_id=source.id,
            split="test",
            expected_class="purchase_order",
            ground_truth={"fields": {"po_number": "PO-1"}, "lines": []},
            actor_id="user:test",
        )
        await publish_dataset_version(
            session, context, dataset=dataset, version=version, actor_id="user:test"
        )
        return stream_version_id, version.id


async def test_server_mode_persists_authenticated_contract_and_rejects_predictions(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    stream_version_id, dataset_version_id = await _seed(harness)
    endpoint = "/orgs/northstar/streams/email/evaluations"
    rejected = client.post(
        endpoint,
        headers=ADMIN,
        json={
            "execution_mode": "server",
            "stream_version_id": str(stream_version_id),
            "dataset_version_id": str(dataset_version_id),
            "predictions": {"a" * 64: {"fields": {"po_number": "forged"}}},
        },
    )
    assert rejected.status_code == 422

    accepted = client.post(
        endpoint,
        headers=ADMIN,
        json={
            "execution_mode": "server",
            "stream_version_id": str(stream_version_id),
            "dataset_version_id": str(dataset_version_id),
        },
    )
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    assert body["execution_mode"] == "server"
    assert body["evidence_source"] is None
    assert body["promotion_eligible"] is False
    assert len(body["execution_fingerprint"]) == 64

    organization_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=organization_id)
    async with db.session_scope() as session:
        stream = await StreamRepository(session, context).get_by_slug("email")
        assert stream is not None
        (run,) = await EvaluationRunRepository(session, context).latest_for_stream(stream.id)
        assert run.execution_mode == "server"
        assert run.predictions == {}
        assert run.candidate_snapshot is not None
        assert run.candidate_snapshot["fingerprint"] == run.candidate_fingerprint
        assert run.runtime_pins is not None
        assert run.runtime_pins["execution_fingerprint"] == run.execution_fingerprint


async def test_simulation_mode_is_explicit_and_never_claims_server_evidence(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _db = harness
    stream_version_id, dataset_version_id = await _seed(harness)
    endpoint = "/orgs/northstar/streams/email/evaluations"
    missing_predictions = client.post(
        endpoint,
        headers=ADMIN,
        json={
            "execution_mode": "simulation",
            "stream_version_id": str(stream_version_id),
            "dataset_version_id": str(dataset_version_id),
        },
    )
    assert missing_predictions.status_code == 422

    accepted = client.post(
        endpoint,
        headers=ADMIN,
        json={
            "execution_mode": "simulation",
            "stream_version_id": str(stream_version_id),
            "dataset_version_id": str(dataset_version_id),
            "predictions": {"a" * 64: {"fields": {"po_number": "PO-1"}}},
        },
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["execution_mode"] == "simulation"
    assert accepted.json()["evidence_source"] == "caller_submitted_predictions"
    assert accepted.json()["promotion_eligible"] is False
