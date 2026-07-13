"""Approval and rejection service tests (REV-012): the approval matrix,
critical-blocker override policy, duplicate-request idempotency, the
second-approval hook, and rejection permissions."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.services.approval import ApprovalPermissionError, approve_document
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import (
    DocumentRepository,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.extracted_fields import Evidence, EvidenceCertainty, create_extracted_field
from soa_db.outbox import OutboxEvent
from soa_db.repository import OrganizationContext
from soa_db.review_tasks import (
    ReviewTaskRepository,
    claim_task,
    release_task,
    route_document_to_review,
)
from soa_db.runs import start_run
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:admin"}
SUPERVISOR = {"X-Dev-User": "user:supervisor"}  # approve + override + reject
REVIEWER = {"X-Dev-User": "user:reviewer"}  # approve, NO override, NO reject


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/approval-api.db")
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
        (REVIEWER, "reviewer@northstar.example", "reviewer"),
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


_SEED_COUNTER = iter(range(100))


async def seed_reviewable(client: TestClient, db: DatabaseSessions) -> str:
    """A document in REVIEW_REQUIRED with an OPEN task over a run whose
    po_number is missing (a BLOCKING rule) and whose header total
    disagrees with the single line (also blocking) — both correctable."""
    index = next(_SEED_COUNTER)
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    stream_id = uuid.UUID(client.get("/orgs/northstar/streams", headers=ADMIN).json()[0]["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        document = await create_document(
            session,
            context,
            stream_id=stream_id,
            source_channel=SourceChannel.UPLOAD,
            original_filename=f"po-approve-{index}.pdf",
            content_sha256=f"{index:x}".rjust(2, "0") * 32,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
        )
        run = await start_run(
            session,
            context,
            document_id=document.id,
            input_sha256="e" * 64,
            stream_version_id=None,
            config_fingerprint=None,
            triggered_by="system:test",
        )
        values: list[tuple[str, str | None, int | None]] = [
            ("po_number", None, None),  # missing: blocking rule fires
            ("order_date", "2026-03-14", None),
            ("customer_name", "Acme", None),
            ("currency", "USD", None),
            ("total_amount", "999.99", None),  # disagrees with the line
            ("lines.sku", "WID-100", 0),
            ("lines.quantity", "10", 0),
            ("lines.unit_price", "45.00", 0),
            ("lines.line_total", "450.00", 0),
        ]
        for key, value, row in values:
            await create_extracted_field(
                session,
                context,
                document_id=document.id,
                run_id=run.id,
                field_key=key,
                raw_value=value,
                confidence=(0.99 if key == "order_date" else 0.9) if value is not None else 0.0,
                provider="mock",
                row_index=row,
                normalized_value=value,
                evidence=(
                    (Evidence(page_number=1, certainty=EvidenceCertainty.PAGE),)
                    if key == "order_date"
                    else ()
                ),
            )
        # Walk the document to REVIEW_REQUIRED the audited way.
        for state in (
            DocumentState.VALIDATING_FILE,
            DocumentState.QUEUED,
            DocumentState.PREPROCESSING,
            DocumentState.CLASSIFYING,
            DocumentState.SPLITTING,
            DocumentState.EXTRACTING,
            DocumentState.NORMALIZING,
            DocumentState.VALIDATING_DATA,
            DocumentState.REVIEW_REQUIRED,
        ):
            await transition_document(
                session, context, document=document, to_state=state, actor_id="system:test"
            )
        task = await route_document_to_review(
            session,
            context,
            document_id=document.id,
            run_id=run.id,
            reasons=[
                {
                    "code": "rule_triggered",
                    "message": "PO number is required",
                    "field_key": None,
                    "row_index": None,
                    "rule_key": "required.po_number",
                }
            ],
            priority=10,
        )
        return str(task.id)


def correct(
    client: TestClient,
    task_id: str,
    version: int,
    headers: dict[str, str],
    **body: object,
) -> int:
    """Apply one correction; returns the new task version."""
    response = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/corrections",
        json={"expected_version": version, **body},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return int(response.json()["task_version"])


def claim(client: TestClient, task_id: str, headers: dict[str, str]) -> int:
    response = client.post(f"/orgs/northstar/review-tasks/{task_id}/claim", headers=headers)
    assert response.status_code == 200, response.text
    return int(response.json()["version"])


async def document_state(
    db: DatabaseSessions, client: TestClient, task_id: str
) -> tuple[str, str | None]:
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        task = await ReviewTaskRepository(session, context).get(uuid.UUID(task_id))
        assert task is not None
        document = await DocumentRepository(session, context).get(task.document_id)
        assert document is not None
        return document.state, document.state_reason


async def test_clean_approval_is_transactional_and_idempotent(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id = await seed_reviewable(client, db)
    version = claim(client, task_id, SUPERVISOR)
    version = correct(client, task_id, version, SUPERVISOR, field_key="po_number", value="PO-1")
    correct(client, task_id, version, SUPERVISOR, field_key="total_amount", value="450.00")

    approved = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve", json={}, headers=SUPERVISOR
    )
    assert approved.status_code == 200, approved.text
    payload = approved.json()
    assert payload["status"] == "approved"
    assert payload["idempotent"] is False
    assert payload["warnings"] == []
    assert payload["override_used"] is False
    assert payload["task"]["state"] == "completed"
    assert payload["task"]["outcome"] == "approved"
    assert (await document_state(db, client, task_id))[0] == "approved"

    # Duplicate request: idempotent success, no second export trigger.
    duplicate = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve", json={}, headers=SUPERVISOR
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["idempotent"] is True
    async with db.session_scope() as session:
        events = (
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.event_type == "document.approved")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["approved_by"].startswith("user:")
        assert events[0].payload["override_used"] is False

    # Rejecting an approved task is a conflict, not a flip.
    rejected = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/reject",
        json={"reason": "changed my mind"},
        headers=SUPERVISOR,
    )
    assert rejected.status_code == 409

    # Approval is in the audit trail.
    history = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/workspace", headers=SUPERVISOR
    ).json()["history"]
    assert any(e["action"] == "document.approved" for e in history)


async def test_remaining_warnings_are_captured_not_blocking(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id = await seed_reviewable(client, db)
    version = claim(client, task_id, SUPERVISOR)
    version = correct(client, task_id, version, SUPERVISOR, field_key="po_number", value="PO-1")
    version = correct(
        client, task_id, version, SUPERVISOR, field_key="total_amount", value="450.00"
    )
    # Clearing customer_name leaves a NON-blocking finding (route_to_review).
    correct(
        client,
        task_id,
        version,
        SUPERVISOR,
        field_key="customer_name",
        value=None,
        reason="illegible",
    )

    approved = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve", json={}, headers=SUPERVISOR
    )
    assert approved.status_code == 200, approved.text
    payload = approved.json()
    assert payload["override_used"] is False
    warning_keys = [w.get("rule_key") for w in payload["warnings"]]
    assert "required.customer_name" in warning_keys

    # The captured warnings travel into the audit record.
    history = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/workspace", headers=SUPERVISOR
    ).json()["history"]
    (approval,) = [e for e in history if e["action"] == "document.approved"]
    audited_keys = [w["rule_key"] for w in approval["summary"]["remaining_warnings"]]
    assert "required.customer_name" in audited_keys


async def test_critical_blockers_require_authorized_override(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id = await seed_reviewable(client, db)
    claim(client, task_id, REVIEWER)

    # Blockers unresolved, no override requested: refused with the rule keys.
    refused = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve", json={}, headers=REVIEWER
    )
    assert refused.status_code == 409
    assert "required.po_number" in refused.json()["error"]["message"]

    # An override reason without the override PERMISSION is still refused.
    unauthorized = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve",
        json={"override_reason": "customer confirmed by phone"},
        headers=REVIEWER,
    )
    assert unauthorized.status_code == 403
    assert "documents.approve.override" in unauthorized.json()["error"]["message"]

    # A non-assignee cannot approve even with every permission.
    not_assignee = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve",
        json={"override_reason": "supervisor override"},
        headers=SUPERVISOR,
    )
    assert not_assignee.status_code == 403
    assert "assignee" in not_assignee.json()["error"]["message"]

    # Hand the task to the supervisor: override now succeeds and is audited.
    release = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/release", json={}, headers=REVIEWER
    )
    assert release.status_code == 200
    claim(client, task_id, SUPERVISOR)
    approved = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve",
        json={"override_reason": "customer confirmed totals by phone"},
        headers=SUPERVISOR,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["override_used"] is True
    assert (await document_state(db, client, task_id))[0] == "approved"
    history = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/workspace", headers=SUPERVISOR
    ).json()["history"]
    (approval,) = [e for e in history if e["action"] == "document.approved"]
    assert approval["summary"]["override_used"] is True
    assert approval["summary"]["override_reason"] == "customer confirmed totals by phone"


async def test_rejection_permissions_reason_and_idempotency(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id = await seed_reviewable(client, db)
    claim(client, task_id, REVIEWER)

    # The reviewer role cannot reject at all (no documents.reject).
    denied = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/reject",
        json={"reason": "not an order"},
        headers=REVIEWER,
    )
    assert denied.status_code == 403

    # The supervisor may reject but is not the assignee yet.
    not_assignee = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/reject",
        json={"reason": "not an order"},
        headers=SUPERVISOR,
    )
    assert not_assignee.status_code == 403

    release = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/release", json={}, headers=REVIEWER
    )
    assert release.status_code == 200
    claim(client, task_id, SUPERVISOR)

    blank = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/reject",
        json={"reason": "   "},
        headers=SUPERVISOR,
    )
    assert blank.status_code == 400

    rejected = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/reject",
        json={"reason": "This is a quote, not a purchase order."},
        headers=SUPERVISOR,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    state, state_reason = await document_state(db, client, task_id)
    assert state == "rejected"
    assert state_reason == "This is a quote, not a purchase order."

    duplicate = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/reject",
        json={"reason": "again"},
        headers=SUPERVISOR,
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["idempotent"] is True

    flipped = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve", json={}, headers=SUPERVISOR
    )
    assert flipped.status_code == 409


async def test_second_approval_hook_demands_a_distinct_approver(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    """The dual-approval seam: when policy demands it, the first approval
    is recorded, the task returns to OPEN, and only a DIFFERENT approver
    can complete it."""
    client, db = harness
    task_id = await seed_reviewable(client, db)
    version = claim(client, task_id, SUPERVISOR)
    version = correct(client, task_id, version, SUPERVISOR, field_key="po_number", value="PO-1")
    correct(client, task_id, version, SUPERVISOR, field_key="total_amount", value="450.00")

    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    always_dual = lambda review: True  # noqa: E731

    async with db.session_scope() as session:
        repo = ReviewTaskRepository(session, context)
        task = await repo.get(uuid.UUID(task_id))
        assert task is not None
        document = await DocumentRepository(session, context).get(task.document_id)
        assert document is not None
        actor = task.assigned_to
        assert actor is not None
        first = await approve_document(
            session,
            context,
            task=task,
            document=document,
            actor=actor,
            can_override=True,
            second_approval=always_dual,
        )
        assert first["status"] == "pending_second_approval"
        assert task.state == "open"
        assert task.first_approved_by == actor

        # The SAME approver cannot finish it.
        await claim_task(session, context, task=task, user_id=actor)
        with pytest.raises(ApprovalPermissionError, match="SECOND approver"):
            await approve_document(
                session,
                context,
                task=task,
                document=document,
                actor=actor,
                can_override=True,
                second_approval=always_dual,
            )
        await release_task(session, context, task=task, actor_id=actor)

        # A different approver completes the approval.
        await claim_task(session, context, task=task, user_id="user:other-supervisor")
        final = await approve_document(
            session,
            context,
            task=task,
            document=document,
            actor="user:other-supervisor",
            can_override=True,
            second_approval=always_dual,
        )
        assert final["status"] == "approved"
        assert task.state == "completed"
        assert document.state == "approved"
