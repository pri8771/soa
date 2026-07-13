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


async def test_workspace_returns_the_full_bounded_read_model(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    from soa_db.extracted_fields import (
        Candidate,
        Evidence,
        EvidenceCertainty,
        create_extracted_field,
    )
    from soa_db.pages import create_page
    from soa_db.runs import start_run

    client, db = harness
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    stream_id = uuid.UUID(client.get("/orgs/northstar/streams", headers=ADMIN).json()[0]["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        document = await create_document(
            session,
            context,
            stream_id=stream_id,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po-workspace.pdf",
            content_sha256="d" * 64,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
        )
        run = await start_run(
            session,
            context,
            document_id=document.id,
            input_sha256="d" * 64,
            stream_version_id=None,
            config_fingerprint="b" * 64,
            triggered_by="system:test",
        )
        image_artifact = uuid.uuid4()
        await create_page(
            session,
            context,
            document_id=document.id,
            run_id=run.id,
            page_number=1,
            width_px=1700,
            height_px=2200,
            dpi=200,
            image_artifact_id=image_artifact,
        )
        await create_extracted_field(
            session,
            context,
            document_id=document.id,
            run_id=run.id,
            field_key="po_number",
            raw_value="PO-100042",
            confidence=0.6,
            provider="mock",
            evidence=(
                Evidence(
                    page_number=1,
                    certainty=EvidenceCertainty.REGION,
                    polygon=((100.0, 200.0), (400.0, 200.0), (400.0, 260.0), (100.0, 260.0)),
                    quote="PO-100042",
                ),
            ),
            candidates=(Candidate("PO-1000A2", 0.35),),
            page_dimensions={1: (1700, 2200)},
        )
        for row, sku in enumerate(["WID-100", "GAD-205"]):
            await create_extracted_field(
                session,
                context,
                document_id=document.id,
                run_id=run.id,
                field_key="lines.sku",
                raw_value=sku,
                confidence=0.9,
                provider="mock",
                row_index=row,
            )
        task = await route_document_to_review(
            session,
            context,
            document_id=document.id,
            run_id=run.id,
            reasons=[
                {
                    "code": "low_confidence",
                    "message": "below the gate",
                    "field_key": "po_number",
                    "row_index": None,
                    "rule_key": None,
                }
            ],
            priority=10,
        )
        task_id = str(task.id)

    response = client.get(f"/orgs/northstar/review-tasks/{task_id}/workspace", headers=SUPERVISOR)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task"]["id"] == task_id
    assert payload["document"]["original_filename"] == "po-workspace.pdf"
    assert payload["run"]["run_number"] == 1
    assert payload["context"]["config_fingerprint"] == "b" * 64

    (po,) = payload["fields"]
    assert po["field_key"] == "po_number"
    assert po["raw_value"] == "PO-100042"
    assert po["candidates"] == [{"raw_value": "PO-1000A2", "confidence": 0.35}]
    (span,) = po["evidence"]
    assert span["page_number"] == 1
    assert span["polygon"] == [[100.0, 200.0], [400.0, 200.0], [400.0, 260.0], [100.0, 260.0]]
    assert span["quote"] == "PO-100042"

    assert [cells[0]["raw_value"] for cells in payload["line_items"]["lines"]] == [
        "WID-100",
        "GAD-205",
    ]
    (page,) = payload["pages"]
    assert page["page_number"] == 1
    assert page["image_artifact_id"]  # artifact ID, not a URL or key
    assert "object_key" not in response.text
    assert "orgs/" not in response.text.replace("/orgs/northstar", "")

    actions = [entry["action"] for entry in payload["history"]]
    assert "review_task.created" in actions
    assert "document.received" in actions

    # Permission enforced: no documents.review, no workspace.
    assert (
        client.get(f"/orgs/northstar/review-tasks/{task_id}/workspace", headers=AUDITOR).status_code
        == 403
    )


async def seed_correctable_task(client: TestClient, db: DatabaseSessions) -> tuple[str, int]:
    """A claimed task over a run whose po_number is missing (a blocking
    rule) and whose total disagrees with the lines — both correctable."""
    from soa_db.extracted_fields import Evidence, EvidenceCertainty, create_extracted_field
    from soa_db.runs import start_run

    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    stream_id = uuid.UUID(client.get("/orgs/northstar/streams", headers=ADMIN).json()[0]["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        document = await create_document(
            session,
            context,
            stream_id=stream_id,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po-correct.pdf",
            content_sha256="e" * 64,
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
                # order_date is CRITICAL: give it a passing confidence so
                # the only review reasons are the two seeded defects.
                confidence=(0.99 if key == "order_date" else 0.9) if value is not None else 0.0,
                provider="mock",
                row_index=row,
                normalized_value=value,
                # Critical fields need evidence to auto-approve (PRC-011).
                evidence=(
                    (Evidence(page_number=1, certainty=EvidenceCertainty.PAGE),)
                    if key == "order_date"
                    else ()
                ),
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
        task_id = str(task.id)
    claimed = client.post(f"/orgs/northstar/review-tasks/{task_id}/claim", headers=SUPERVISOR)
    assert claimed.status_code == 200
    return task_id, claimed.json()["version"]


def correct(
    client: TestClient,
    task_id: str,
    version: int,
    headers: dict[str, str],
    **body: object,
) -> object:
    payload = {"expected_version": version, **body}
    return client.post(
        f"/orgs/northstar/review-tasks/{task_id}/corrections", json=payload, headers=headers
    )


async def test_corrections_append_normalize_and_revalidate(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id, version = await seed_correctable_task(client, db)

    # Fix the missing PO number: the blocking rule stops firing.
    first = correct(
        client,
        task_id,
        version,
        SUPERVISOR,
        field_key="po_number",
        value=" po-100042 ",
        reason="read from the document",
    )
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["correction"]["corrected_raw_value"] == " po-100042 "
    assert payload["correction"]["corrected_normalized_value"] == "PO-100042"
    assert payload["task_version"] > version
    reasons = [r["code"] for r in payload["revalidation"]["decision"]["reasons"]]
    assert "rule_triggered" in reasons  # the totals mismatch still stands

    # Fix the total: the run now validates clean and would approve.
    second = correct(
        client,
        task_id,
        payload["task_version"],
        SUPERVISOR,
        field_key="total_amount",
        value="450.00",
        reason="header total was misread",
    )
    assert second.status_code == 200, second.text
    revalidation = second.json()["revalidation"]
    assert revalidation["decision"]["route"] == "approved", revalidation["decision"]["reasons"]
    assert revalidation["decision"]["reasons"] == []
    assert revalidation["evaluation"]["blocking"] is False

    # The original extraction is untouched (immutable), the audit trail
    # has both corrections.
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        from soa_db.extracted_fields import ExtractedFieldRepository
        from soa_db.review_tasks import ReviewTaskRepository

        task = await ReviewTaskRepository(session, context).get(uuid.UUID(task_id))
        assert task is not None
        fields = await ExtractedFieldRepository(session, context).list_for_run(task.run_id)
        by_key = {(f.field_key, f.row_index): f for f in fields}
        assert by_key[("po_number", None)].raw_value is None  # original retained
        assert by_key[("total_amount", None)].raw_value == "999.99"
        assert by_key[("total_amount", None)].validation_status == "passed"
    detail_timeline = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/workspace", headers=SUPERVISOR
    ).json()["history"]
    corrected = [e for e in detail_timeline if e["action"] == "review.field_corrected"]
    assert len(corrected) == 2


async def test_stale_version_is_refused_without_saving(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id, version = await seed_correctable_task(client, db)
    ok = correct(
        client,
        task_id,
        version,
        SUPERVISOR,
        field_key="po_number",
        value="PO-1",
    )
    assert ok.status_code == 200
    # A second editor still holding the old version is refused loudly.
    stale = correct(
        client,
        task_id,
        version,
        SUPERVISOR,
        field_key="po_number",
        value="PO-2",
    )
    assert stale.status_code == 409
    assert "Reload before editing" in stale.json()["error"]["message"]


async def test_only_the_assignee_may_correct(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id, version = await seed_correctable_task(client, db)
    denied = correct(
        client,
        task_id,
        version,
        ADMIN,
        field_key="po_number",
        value="PO-1",
    )
    assert denied.status_code == 403
    unknown_field = correct(
        client,
        task_id,
        version,
        SUPERVISOR,
        field_key="nonexistent",
        value="x",
    )
    assert unknown_field.status_code == 404


async def test_unnormalizable_corrections_are_kept_with_their_error(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id, version = await seed_correctable_task(client, db)
    response = correct(
        client,
        task_id,
        version,
        SUPERVISOR,
        field_key="order_date",
        value="next tuesday",
    )
    assert response.status_code == 200
    correction = response.json()["correction"]
    assert correction["corrected_normalized_value"] is None
    assert "unrecognized date format" in correction["normalization_error"]


async def test_new_row_and_cleared_row_corrections(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    """REV-008 row edits: a correction may target a NEW table row (add /
    split), and clearing every cell of a row removes it from the rules'
    view of the table."""
    client, db = harness
    task_id, version = await seed_correctable_task(client, db)
    # Complete the header first so only line math matters.
    for field_key, value in (("po_number", "PO-1"), ("total_amount", "900.00")):
        response = correct(client, task_id, version, SUPERVISOR, field_key=field_key, value=value)
        assert response.status_code == 200, response.text
        version = response.json()["task_version"]

    # Add a second line as corrections on a row that was never extracted.
    for field_key, value in (
        ("lines.sku", "GAD-205"),
        ("lines.quantity", "10"),
        ("lines.unit_price", "45.00"),
        ("lines.line_total", "450.00"),
    ):
        response = correct(
            client,
            task_id,
            version,
            SUPERVISOR,
            field_key=field_key,
            value=value,
            row_index=1,
        )
        assert response.status_code == 200, response.text
        version = response.json()["task_version"]
    revalidation = response.json()["revalidation"]
    # Two 450.00 lines reconcile against the corrected 900.00 total.
    assert revalidation["decision"]["route"] == "approved"

    # Header fields can NOT be invented the same way.
    bogus = correct(
        client,
        task_id,
        version,
        SUPERVISOR,
        field_key="nonexistent",
        value="x",
        row_index=2,
    )
    assert bogus.status_code == 404

    # Remove the added row: clear every cell; the total then mismatches
    # again and the run goes back to review.
    for field_key in ("lines.sku", "lines.quantity", "lines.unit_price", "lines.line_total"):
        response = correct(
            client,
            task_id,
            version,
            SUPERVISOR,
            field_key=field_key,
            value=None,
            row_index=1,
            reason="row removed",
        )
        assert response.status_code == 200, response.text
        version = response.json()["task_version"]
    revalidation = response.json()["revalidation"]
    assert revalidation["decision"]["route"] == "review_required"
    codes = [r["rule_key"] for r in revalidation["decision"]["reasons"] if r["rule_key"]]
    assert "totals.header_matches_lines" in codes
