"""Operations dashboard API tests (ANA-004): the snapshot shape with
definitions and attention items (each carrying its drill-down target),
window validation, and tenancy."""

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import SourceChannel, create_document
from soa_db.repository import OrganizationContext
from soa_db.review_tasks import route_document_to_review
from soa_db.types import utcnow
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:admin"}


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/analytics-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(
        ApiSettings(environment=Environment.TEST), db=db, object_store=MemoryObjectStore()
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
    return client, db


async def seed(client: TestClient, db: DatabaseSessions) -> None:
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    stream_id = uuid.UUID(client.get("/orgs/northstar/streams", headers=ADMIN).json()[0]["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        document = await create_document(
            session,
            context,
            stream_id=stream_id,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256="a" * 64,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
        )
        await route_document_to_review(
            session,
            context,
            document_id=document.id,
            run_id=uuid.uuid4(),
            reasons=[
                {
                    "code": "low_confidence",
                    "message": "below the gate",
                    "field_key": "po_number",
                }
            ],
            priority=10,
            blocking=True,
            sla_due_at=utcnow() - timedelta(hours=1),
        )


async def test_snapshot_shape_attention_links_and_definitions(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    await seed(client, db)
    response = client.get("/orgs/northstar/analytics/operations", headers=ADMIN)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["window"]["timezone"] == "UTC"
    assert payload["backlog"]["review_tasks"] == {"open": 1, "in_progress": 0, "blocking": 1}
    assert payload["sla"]["overdue_now"] == 1
    assert payload["volume"]["per_day"][0]["count"] == 1

    # Every attention item names a drill-down target — never a bare number.
    by_key = {item["key"]: item for item in payload["needs_attention"]}
    assert by_key["overdue_reviews"]["count"] == 1
    assert by_key["overdue_reviews"]["link"] == {
        "screen": "review",
        "filters": {"view": "overdue"},
    }
    assert by_key["blocked_reviews"]["count"] == 1
    for item in payload["needs_attention"]:
        assert item["link"]["screen"]
        assert "filters" in item["link"]

    # Definitions travel on the payload.
    assert payload["definitions"]["sla.overdue_now"]["numerator"]
    assert payload["definitions"]["exceptions.documents_by_state"]["denominator"]


async def test_window_validation_and_tenancy(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    backwards = client.get(
        "/orgs/northstar/analytics/operations",
        params={"since": "2026-07-10T00:00:00+00:00", "until": "2026-07-01T00:00:00+00:00"},
        headers=ADMIN,
    )
    assert backwards.status_code == 400
    malformed = client.get(
        "/orgs/northstar/analytics/operations", params={"since": "yesterday"}, headers=ADMIN
    )
    assert malformed.status_code == 400
    too_wide = client.get(
        "/orgs/northstar/analytics/operations",
        params={"since": "2020-01-01T00:00:00+00:00", "until": "2026-07-01T00:00:00+00:00"},
        headers=ADMIN,
    )
    assert too_wide.status_code == 400
    # A non-member cannot even see the organization.
    outsider = {"X-Dev-User": "user:integration-admin"}
    assert client.get("/orgs/northstar/analytics/operations", headers=outsider).status_code == 404


async def test_quality_snapshot_shape_and_honesty(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    await seed(client, db)
    response = client.get("/orgs/northstar/analytics/quality", headers=ADMIN)
    assert response.status_code == 200, response.text
    payload = response.json()
    # No reviewed tasks yet: rates are None/empty, never flattering zeros.
    assert payload["reviewed"]["tasks_completed"] == 0
    assert payload["field_corrections"] == []
    assert payload["stp"]["stp_rate"] is None
    assert payload["false_auto_approval"]["available"] is False
    assert "gold" in payload["false_auto_approval"]["reason"]
    assert payload["ground_truth"]["gold_documents"] == 0
    assert payload["definitions"]["quality.field_correction_rate"]["numerator"]
    # Same window validation as operations.
    assert (
        client.get(
            "/orgs/northstar/analytics/quality",
            params={"since": "not-a-date"},
            headers=ADMIN,
        ).status_code
        == 400
    )


async def test_usage_snapshot_labels_and_quota_honesty(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    from soa_db.usage_ledger import record_usage

    client, db = harness
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    async with db.session_scope() as session:
        await record_usage(
            session,
            OrganizationContext(organization_id=org_id),
            provider="hosted-ocr",
            cost_category="ocr",
            estimated_cost_cents=120,
            billed_unit="pages",
            billed_quantity=12,
            actor_id="system:worker",
        )
    response = client.get("/orgs/northstar/analytics/usage", headers=ADMIN)
    assert response.status_code == 200, response.text
    payload = response.json()
    (group,) = payload["groups"]
    # Estimated and reconciled stay separate, billed units are facts.
    assert group["estimated_cents"] == 120
    assert group["reconciled_cents"] == 120
    assert (group["billed_unit"], group["billed_quantity"]) == ("pages", 12)
    assert payload["semantics"]["reconciled_cents"].startswith("estimated + appended")
    # No quota policy yet: stated, not faked.
    assert payload["quotas"]["configured"] is False
    assert "ANA-009" in payload["quotas"]["reason"]
    # Provider appears by name only — nothing else identifies an account.
    assert set(group) == {
        "stream_id",
        "provider",
        "provider_model",
        "cost_category",
        "billed_unit",
        "billed_quantity",
        "pages",
        "entries",
        "estimated_cents",
        "adjustment_cents",
        "reconciled_cents",
    }
