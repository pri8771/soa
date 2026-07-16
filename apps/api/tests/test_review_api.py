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
    assert payload["superseded_by_task_id"] is None  # this IS the active task
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


async def test_workspace_of_a_superseded_task_points_at_the_current_active_task(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    """A reprocess cancels the old task and opens a fresh one for the new
    run; the old task's workspace names the current active task so a stale
    URL can redirect to the live review."""
    from soa_db.runs import start_run

    client, db = harness
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    stream_id = uuid.UUID(client.get("/orgs/northstar/streams", headers=ADMIN).json()[0]["id"])
    context = OrganizationContext(organization_id=org_id)

    async def route(run_id: uuid.UUID) -> uuid.UUID:
        task = await route_document_to_review(
            session,
            context,
            document_id=document.id,
            run_id=run_id,
            reasons=[_reason()],
            priority=10,
        )
        return task.id

    async with db.session_scope() as session:
        document = await create_document(
            session,
            context,
            stream_id=stream_id,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po-super.pdf",
            content_sha256="e" * 64,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
        )
        run1 = await start_run(
            session,
            context,
            document_id=document.id,
            input_sha256="e" * 64,
            stream_version_id=None,
            config_fingerprint="b" * 64,
            triggered_by="system:test",
        )
        old_task_id = await route(run1.id)
        # A reprocess: a second run supersedes the first task with a fresh one.
        run2 = await start_run(
            session,
            context,
            document_id=document.id,
            input_sha256="e" * 64,
            stream_version_id=None,
            config_fingerprint="b" * 64,
            triggered_by="user:reprocess",
        )
        new_task_id = await route(run2.id)

    old = client.get(
        f"/orgs/northstar/review-tasks/{old_task_id}/workspace", headers=SUPERVISOR
    ).json()
    assert old["task"]["state"] == "cancelled"
    assert old["superseded_by_task_id"] == str(new_task_id)

    new = client.get(
        f"/orgs/northstar/review-tasks/{new_task_id}/workspace", headers=SUPERVISOR
    ).json()
    assert new["task"]["state"] == "open"
    assert new["superseded_by_task_id"] is None


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


async def test_comments_are_tenant_scoped_audited_and_body_free_in_notifications(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    """REV-011: the comment thread round-trips with mentions extracted,
    lands in the audit trail, and the outbox notification carries ids
    only — NEVER the comment body."""
    from sqlalchemy import select

    from soa_db.outbox import OutboxEvent

    client, db = harness
    (task_id,) = await seed_tasks(client, db, count=1)

    body = "Totals look off — @admin can you check the freight line? Contains ACME-PO-77."
    created = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/comments",
        json={"body": body},
        headers=SUPERVISOR,
    )
    assert created.status_code == 201, created.text
    comment = created.json()
    assert comment["body"] == body
    assert comment["mentions"] == ["admin"]
    assert comment["author"].startswith("user:")

    # Any reviewer in the org reads the thread; ordering is oldest first.
    second = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/comments",
        json={"body": "Looking now."},
        headers=ADMIN,
    )
    assert second.status_code == 201
    listing = client.get(f"/orgs/northstar/review-tasks/{task_id}/comments", headers=ADMIN).json()
    assert [c["body"] for c in listing["items"]] == [body, "Looking now."]

    # Audited — with length only, never the text.
    history = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/workspace", headers=SUPERVISOR
    ).json()["history"]
    audited = [e for e in history if e["action"] == "review.comment_added"]
    assert len(audited) == 2
    assert audited[0]["summary"] == {"comment_id": comment["id"], "length": len(body)}
    assert "ACME-PO-77" not in str(audited)

    # The notification event exists but does NOT contain the body.
    async with db.session_scope() as session:
        events = (
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.event_type == "review.comment_added")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 2
        first_event = next(e for e in events if e.payload["comment_id"] == comment["id"])
        assert first_event.payload["task_id"] == task_id
        assert first_event.payload["mentions"] == ["admin"]
        assert "body" not in first_event.payload
        assert "ACME-PO-77" not in str(first_event.payload)

    # Whitespace-only bodies are refused before anything is written.
    blank = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/comments",
        json={"body": "   "},
        headers=SUPERVISOR,
    )
    assert blank.status_code == 400

    # Permission matrix: no documents.review, no thread; unknown task 404.
    assert (
        client.get(f"/orgs/northstar/review-tasks/{task_id}/comments", headers=AUDITOR).status_code
        == 403
    )
    assert (
        client.post(
            f"/orgs/northstar/review-tasks/{task_id}/comments",
            json={"body": "x"},
            headers=AUDITOR,
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/orgs/northstar/review-tasks/{uuid.uuid4()}/comments", headers=SUPERVISOR
        ).status_code
        == 404
    )


async def test_escalation_releases_reprioritizes_and_records_ownership(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    """REV-011 escalation: an in-progress task returns to OPEN with the
    reason and escalator recorded and the priority raised, so a
    supervisor can claim it; settled tasks refuse escalation."""
    client, db = harness
    task_ids = await seed_tasks(client, db, count=2)
    task_id = task_ids[0]  # priority 30

    claimed = client.post(f"/orgs/northstar/review-tasks/{task_id}/claim", headers=SUPERVISOR)
    assert claimed.status_code == 200

    escalated = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/escalate",
        json={"reason": "Handwritten totals; needs a supervisor decision."},
        headers=SUPERVISOR,
    )
    assert escalated.status_code == 200, escalated.text
    payload = escalated.json()
    assert payload["state"] == "open"
    assert payload["assigned_to"] is None
    assert payload["priority"] == 10  # jumped the queue from 30
    assert payload["escalated_by"].startswith("user:")
    assert payload["escalation_reason"] == "Handwritten totals; needs a supervisor decision."
    assert payload["escalated_at"] is not None

    # The escalated task is claimable again — by someone else.
    reclaimed = client.post(f"/orgs/northstar/review-tasks/{task_id}/claim", headers=ADMIN)
    assert reclaimed.status_code == 200
    assert reclaimed.json()["escalation_reason"] is not None

    # Escalation is audited.
    history = client.get(f"/orgs/northstar/review-tasks/{task_id}/workspace", headers=ADMIN).json()[
        "history"
    ]
    audited = [e for e in history if e["action"] == "review_task.escalated"]
    assert len(audited) == 1
    assert audited[0]["summary"]["priority"] == 10

    # A settled task cannot be escalated: 409, not a silent reopen.
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        from soa_db.review_tasks import ReviewTaskRepository, complete_task

        task = await ReviewTaskRepository(session, context).get(uuid.UUID(task_id))
        assert task is not None
        await complete_task(session, context, task=task, outcome="approved", actor_id="user:test")
    settled = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/escalate",
        json={"reason": "too late"},
        headers=SUPERVISOR,
    )
    assert settled.status_code == 409

    # Permission matrix + blank reasons.
    other = task_ids[1]
    assert (
        client.post(
            f"/orgs/northstar/review-tasks/{other}/escalate",
            json={"reason": "x"},
            headers=AUDITOR,
        ).status_code
        == 403
    )
    blank = client.post(
        f"/orgs/northstar/review-tasks/{other}/escalate",
        json={"reason": "   "},
        headers=SUPERVISOR,
    )
    assert blank.status_code == 400


# --- Catalog candidate matching in review (CAT-010) ---


async def bind_catalogs(client: TestClient, db: DatabaseSessions) -> None:
    """A products catalog (for lines.sku) and a customers catalog (for
    customer_name), both activated and rolling-bound to the stream."""
    from soa_db.catalogs import (
        CatalogBindingMode,
        activate_catalog_version,
        add_catalog_record,
        bind_catalog_to_stream,
        create_catalog,
        create_catalog_version,
    )

    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    stream_id = uuid.UUID(client.get("/orgs/northstar/streams", headers=ADMIN).json()[0]["id"])
    context = OrganizationContext(organization_id=org_id)
    catalogs: list[tuple[str, str, list[tuple[str, str, list[str]]]]] = [
        (
            "products",
            "products",
            [
                ("WID-100", "Widget 100 (steel)", ["WIDGET 100"]),
                ("WID-200", "Widget 200 (alloy)", []),
                ("GAD-205", "Gadget 205", []),
            ],
        ),
        ("customers", "customers", [("CUST-1", "Acme GmbH", ["Acme"])]),
    ]
    async with db.session_scope() as session:
        for slug, catalog_type, records in catalogs:
            catalog = await create_catalog(
                session,
                context,
                name=slug.title(),
                slug=slug,
                catalog_type=catalog_type,
                source="manual",
                actor_id="user:test",
            )
            version = await create_catalog_version(
                session, context, catalog=catalog, actor_id="user:test"
            )
            for source_id, name, aliases in records:
                await add_catalog_record(
                    session,
                    context,
                    version=version,
                    source_id=source_id,
                    display_name=name,
                    aliases=aliases,
                )
            await activate_catalog_version(
                session, context, catalog=catalog, version=version, actor_id="user:test"
            )
            await bind_catalog_to_stream(
                session,
                context,
                stream_id=stream_id,
                catalog=catalog,
                mode=CatalogBindingMode.ROLLING,
                actor_id="user:test",
            )


async def test_catalog_candidates_rank_with_explanations(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id, _ = await seed_correctable_task(client, db)
    await bind_catalogs(client, db)

    # An exact identifier hit: score 1.0 with the tier as the explanation.
    exact = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/catalog-candidates",
        params={"field_key": "lines.sku", "q": "WID-100"},
        headers=SUPERVISOR,
    )
    assert exact.status_code == 200, exact.text
    payload = exact.json()
    assert payload["available"] is True
    assert payload["outcome"] == "auto_matched"
    assert payload["machine_selected_source_id"] == "WID-100"
    top = payload["candidates"][0]
    assert (top["code"], top["score"]) == ("WID-100", 1.0)
    assert top["features"][0]["name"] == "exact_identifier"
    assert top["features"][0]["explanation"]

    # A fuzzy query: every candidate carries per-feature explanations.
    fuzzy = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/catalog-candidates",
        params={"field_key": "lines.sku", "q": "Widgt 100 steel"},
        headers=SUPERVISOR,
    ).json()
    assert fuzzy["candidates"], fuzzy["reasons"]
    assert fuzzy["candidates"][0]["code"] == "WID-100"
    for candidate in fuzzy["candidates"]:
        assert {f["name"] for f in candidate["features"]} == {
            "identifier",
            "text_trigram",
            "text_sequence",
        }
        assert all(f["explanation"] for f in candidate["features"])

    # Fields the catalog does not match are refused with the list.
    refused = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/catalog-candidates",
        params={"field_key": "po_number", "q": "x"},
        headers=SUPERVISOR,
    )
    assert refused.status_code == 400
    assert "customer_name" in refused.json()["error"]["message"]


async def test_catalog_candidates_without_a_binding_are_honest(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id, _ = await seed_correctable_task(client, db)
    response = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/catalog-candidates",
        params={"field_key": "lines.sku", "q": "WID-100"},
        headers=SUPERVISOR,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert "no products catalog bound" in payload["reason"]
    assert payload["candidates"] == []


async def test_catalog_selection_corrects_audits_and_revalidates(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id, version = await seed_correctable_task(client, db)
    await bind_catalogs(client, db)

    # Picking the machine's best candidate needs no override reason.
    picked = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/catalog-selection",
        json={
            "field_key": "lines.sku",
            "row_index": 0,
            "query": "WID-100",
            "selected_source_id": "WID-100",
            "expected_version": version,
        },
        headers=SUPERVISOR,
    )
    assert picked.status_code == 200, picked.text
    payload = picked.json()
    assert payload["override"] is False
    # lines.sku is an identifier field: the correction is the source id.
    assert payload["correction"]["corrected_raw_value"] == "WID-100"
    assert payload["selection"]["catalog_record_id"]
    assert payload["selection"]["catalog_version_id"]
    assert payload["selection"]["source_id"] == "WID-100"
    assert payload["task_version"] > version
    # Dependent validation recalculated: the fresh decision came back.
    assert payload["revalidation"]["decision"]["route"] in ("approved", "review_required")

    # customer_name is a text field: the correction is the display name.
    named = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/catalog-selection",
        json={
            "field_key": "customer_name",
            "row_index": None,
            "query": "Acme",
            "selected_source_id": "CUST-1",
            "expected_version": payload["task_version"],
        },
        headers=SUPERVISOR,
    )
    assert named.status_code == 200, named.text
    assert named.json()["correction"]["corrected_raw_value"] == "Acme GmbH"

    # The audit trail retains the FULL decision record for each pick.
    history = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/workspace", headers=SUPERVISOR
    ).json()["history"]
    selections = [e for e in history if e["action"] == "catalog.match_selected"]
    assert len(selections) == 2
    decision = selections[0]["summary"]["decision"]
    assert decision["outcome"] == "auto_matched"
    assert decision["config_fingerprint"]
    assert decision["exact_candidates"][0]["reason"]
    assert selections[0]["summary"]["override"] is False


async def test_catalog_override_requires_a_reason_and_becomes_a_labeled_example(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id, version = await seed_correctable_task(client, db)
    await bind_catalogs(client, db)

    def select(
        source_id: str | None,
        reason: str | None,
        expected: int,
        query: str = "WID-100",
    ) -> object:
        body: dict[str, object] = {
            "field_key": "lines.sku",
            "row_index": 0,
            "query": query,
            "selected_source_id": source_id,
            "expected_version": expected,
        }
        if reason is not None:
            body["reason"] = reason
        return client.post(
            f"/orgs/northstar/review-tasks/{task_id}/catalog-selection",
            json=body,
            headers=SUPERVISOR,
        )

    # Disagreeing with the machine without a written reason is refused.
    refused = select("GAD-205", None, version)
    assert refused.status_code == 400
    assert "written reason" in refused.json()["error"]["message"]

    # With a reason the override lands and is labeled for evaluation.
    overridden = select("GAD-205", "the buyer's WID-100 is the gadget bundle", version)
    assert overridden.status_code == 200, overridden.text
    assert overridden.json()["override"] is True
    assert overridden.json()["correction"]["corrected_raw_value"] == "GAD-205"

    # Confirming NO match (with a reason) records the label, no correction.
    confirmed = select(
        None,
        "free-text item; not in the catalog",
        overridden.json()["task_version"],
        query="GAD-205",
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["correction"] is None
    assert confirmed.json()["selection"]["status"] == "confirmed_no_match"
    assert confirmed.json()["revalidation"]["catalog_identity"]["confirmed_no_match"] == 1

    history = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/workspace", headers=SUPERVISOR
    ).json()["history"]
    examples = [
        e["summary"]["evaluation_example"]
        for e in history
        if e["action"] == "catalog.match_selected" and "evaluation_example" in e["summary"]
    ]
    assert len(examples) == 2
    assert examples[0]["kind"] == "catalog_match_override"
    assert examples[0]["human_selected_source_id"] == "GAD-205"
    assert examples[0]["machine_selected_source_id"] == "WID-100"
    assert examples[0]["agreed"] is False
    assert examples[1]["human_selected_source_id"] is None

    # A record outside the bound version is refused.
    stale = select("SKU-DELETED", "why not", confirmed.json()["task_version"])
    assert stale.status_code == 400
    assert "not in the bound catalog version" in stale.json()["error"]["message"]


async def test_catalog_selection_guards_assignment_and_version(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id, version = await seed_correctable_task(client, db)
    await bind_catalogs(client, db)
    body = {
        "field_key": "lines.sku",
        "row_index": 0,
        "query": "WID-100",
        "selected_source_id": "WID-100",
        "expected_version": version,
    }
    # Only the assignee may pick.
    denied = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/catalog-selection", json=body, headers=ADMIN
    )
    assert denied.status_code == 403
    # A stale version is a loud 409, nothing saved.
    stale = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/catalog-selection",
        json={**body, "expected_version": version + 7},
        headers=SUPERVISOR,
    )
    assert stale.status_code == 409
    assert "nothing was saved" in stale.json()["error"]["message"]


async def test_relevant_free_form_correction_replaces_or_invalidates_catalog_identity(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    from soa_db.catalog_selections import (
        CatalogFieldSelectionRepository,
        latest_catalog_selections,
    )
    from soa_db.review_tasks import ReviewTaskRepository

    client, db = harness
    task_id, version = await seed_correctable_task(client, db)
    await bind_catalogs(client, db)
    picked = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/catalog-selection",
        json={
            "field_key": "lines.sku",
            "row_index": 0,
            "query": "WID-100",
            "selected_source_id": "WID-100",
            "expected_version": version,
        },
        headers=SUPERVISOR,
    )
    assert picked.status_code == 200, picked.text

    changed = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/corrections",
        json={
            "field_key": "lines.sku",
            "row_index": 0,
            "value": "GAD-205",
            "expected_version": picked.json()["task_version"],
        },
        headers=SUPERVISOR,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["revalidation"]["catalog_identity"]["selected"] == 1

    unresolved = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/corrections",
        json={
            "field_key": "lines.sku",
            "row_index": 0,
            "value": "NOT-IN-CATALOG",
            "expected_version": changed.json()["task_version"],
        },
        headers=SUPERVISOR,
    )
    assert unresolved.status_code == 200, unresolved.text
    catalog = unresolved.json()["revalidation"]["catalog_identity"]
    assert any(issue["code"] == "selection_unresolved" for issue in catalog["issues"])
    assert unresolved.json()["revalidation"]["evaluation"]["blocking"] is True

    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        task = await ReviewTaskRepository(session, context).get(uuid.UUID(task_id))
        assert task is not None
        rows = await CatalogFieldSelectionRepository(session, context).list_for_run(task.run_id)
        latest = latest_catalog_selections(rows)[("lines.sku", 0)]
        assert latest.status == "needs_review"
        assert latest.matched_value == "NOT-IN-CATALOG"
