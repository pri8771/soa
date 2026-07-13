"""Review queue API tests (REV-002): views, sorts, cursor discipline,
atomic claim under contention, release rules, and queue invisibility."""

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
SUPERVISOR = {"X-Dev-User": "user:supervisor"}  # has documents.review
AUDITOR = {"X-Dev-User": "user:auditor"}  # read-only: NO documents.review


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/review-api.db")
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
    for headers, email, role in (
        (SUPERVISOR, "supervisor@northstar.example", "supervisor"),
        (AUDITOR, "auditor@northstar.example", "auditor"),
    ):
        client.post("/orgs/northstar/invitations", json={"email": email}, headers=ADMIN)
        accepted = client.post(
            "/invitations/accept", json={"organization_slug": "northstar"}, headers=headers
        )
        granted = client.post(
            f"/orgs/northstar/members/{accepted.json()['membership_id']}/roles",
            json={"role_slug": role},
            headers=ADMIN,
        )
        assert granted.status_code == 201, granted.text
    return client, db


def _reason(field_key: str = "po_number") -> dict[str, object]:
    return {
        "code": "low_confidence",
        "message": "below the gate",
        "field_key": field_key,
        "row_index": None,
        "rule_key": None,
    }


async def seed_tasks(client: TestClient, db: DatabaseSessions, count: int = 3) -> list[str]:
    """``count`` open tasks over distinct documents: priorities 30/20/10
    (created in that order), the LAST one blocking with an expired SLA."""
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    stream_id = uuid.UUID(client.get("/orgs/northstar/streams", headers=ADMIN).json()[0]["id"])
    context = OrganizationContext(organization_id=org_id)
    task_ids: list[str] = []
    async with db.session_scope() as session:
        for index in range(count):
            document = await create_document(
                session,
                context,
                stream_id=stream_id,
                source_channel=SourceChannel.UPLOAD,
                original_filename=f"po-{index:03}.pdf",
                content_sha256=f"{index:x}" * 64,
                size_bytes=100,
                content_type="application/pdf",
                actor_id="user:test",
            )
            last = index == count - 1
            task = await route_document_to_review(
                session,
                context,
                document_id=document.id,
                run_id=uuid.uuid4(),
                reasons=[_reason()],
                priority=30 - index * 10,
                blocking=last,
                sla_due_at=utcnow() - timedelta(hours=1) if last else None,
            )
            task_ids.append(str(task.id))
    return task_ids


async def test_views_and_priority_sort(harness: tuple[TestClient, DatabaseSessions]) -> None:
    client, db = harness
    task_ids = await seed_tasks(client, db)

    listing = client.get("/orgs/northstar/review-tasks", headers=SUPERVISOR).json()
    # Priority sort: lowest number first — the last-created task (10) leads.
    assert [t["id"] for t in listing["items"]] == [task_ids[2], task_ids[1], task_ids[0]]
    assert listing["items"][0]["document_filename"] == "po-002.pdf"
    assert listing["items"][0]["reasons"][0]["field_key"] == "po_number"

    unassigned = client.get(
        "/orgs/northstar/review-tasks?view=unassigned", headers=SUPERVISOR
    ).json()
    assert len(unassigned["items"]) == 3

    overdue = client.get("/orgs/northstar/review-tasks?view=overdue", headers=SUPERVISOR).json()
    assert [t["id"] for t in overdue["items"]] == [task_ids[2]]

    blocked = client.get("/orgs/northstar/review-tasks?view=blocked", headers=SUPERVISOR).json()
    assert [t["id"] for t in blocked["items"]] == [task_ids[2]]
    assert blocked["items"][0]["blocking"] is True

    mine = client.get("/orgs/northstar/review-tasks?view=mine", headers=SUPERVISOR).json()
    assert mine["items"] == []


async def test_atomic_claim_exactly_one_winner(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_ids = await seed_tasks(client, db, count=1)
    task_id = task_ids[0]

    won = client.post(f"/orgs/northstar/review-tasks/{task_id}/claim", headers=SUPERVISOR)
    assert won.status_code == 200, won.text
    assert won.json()["state"] == "in_progress"
    assert won.json()["assigned_to"].startswith("user:")

    # The second claimer loses with an explanation, not a silent steal.
    lost = client.post(f"/orgs/northstar/review-tasks/{task_id}/claim", headers=ADMIN)
    assert lost.status_code == 409
    assert "assigned to" in lost.json()["error"]["message"]

    mine = client.get("/orgs/northstar/review-tasks?view=mine", headers=SUPERVISOR).json()
    assert [t["id"] for t in mine["items"]] == [task_id]


async def test_claim_next_walks_the_priority_order(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_ids = await seed_tasks(client, db)
    first = client.post("/orgs/northstar/review-tasks/claim-next", headers=SUPERVISOR).json()
    assert first["task"]["id"] == task_ids[2]  # priority 10
    assert "Highest-priority" in first["explanation"]
    second = client.post("/orgs/northstar/review-tasks/claim-next", headers=ADMIN).json()
    assert second["task"]["id"] == task_ids[1]  # priority 20: the next one
    # Exhaust the queue.
    client.post("/orgs/northstar/review-tasks/claim-next", headers=SUPERVISOR)
    empty = client.post("/orgs/northstar/review-tasks/claim-next", headers=SUPERVISOR).json()
    assert empty["task"] is None


async def test_release_is_assignee_only(harness: tuple[TestClient, DatabaseSessions]) -> None:
    client, db = harness
    (task_id,) = await seed_tasks(client, db, count=1)
    assert (
        client.post(f"/orgs/northstar/review-tasks/{task_id}/claim", headers=SUPERVISOR).status_code
        == 200
    )
    denied = client.post(f"/orgs/northstar/review-tasks/{task_id}/release", json={}, headers=ADMIN)
    assert denied.status_code == 403
    released = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/release", json={}, headers=SUPERVISOR
    )
    assert released.status_code == 200, released.text
    assert released.json()["state"] == "open"
    assert released.json()["assigned_to"] is None
    # Releasing an open task is a 409, not a crash.
    again = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/release", json={}, headers=SUPERVISOR
    )
    assert again.status_code == 403  # no longer the assignee either


async def test_cursor_is_bound_to_its_listing(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    await seed_tasks(client, db)
    page = client.get(
        "/orgs/northstar/review-tasks?sort=created&limit=1", headers=SUPERVISOR
    ).json()
    assert page["has_more"] is True
    cursor = page["next_cursor"]
    assert cursor is not None
    # Replaying the cursor under different filters is refused.
    crossed = client.get(
        f"/orgs/northstar/review-tasks?sort=created&view=blocked&limit=1&cursor={cursor}",
        headers=SUPERVISOR,
    )
    assert crossed.status_code == 400
    # Priority sort refuses cursors honestly instead of skipping rows.
    refused = client.get(
        f"/orgs/northstar/review-tasks?sort=priority&cursor={cursor}", headers=SUPERVISOR
    )
    assert refused.status_code == 400
    # The same cursor continues its own listing.
    ok = client.get(
        f"/orgs/northstar/review-tasks?sort=created&limit=2&cursor={cursor}",
        headers=SUPERVISOR,
    )
    assert ok.status_code == 200
    assert len(ok.json()["items"]) == 2


async def test_queue_requires_review_permission_and_membership(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    (task_id,) = await seed_tasks(client, db, count=1)
    # A member without documents.review sees 403 — the queue is not theirs.
    assert client.get("/orgs/northstar/review-tasks", headers=AUDITOR).status_code == 403
    assert (
        client.post(f"/orgs/northstar/review-tasks/{task_id}/claim", headers=AUDITOR).status_code
        == 403
    )
    # A non-member cannot even see the organization: 404, not an empty list.
    outsider = {"X-Dev-User": "user:integration-admin"}
    assert client.get("/orgs/northstar/review-tasks", headers=outsider).status_code == 404
