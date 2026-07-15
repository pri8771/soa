"""Approval and rejection service tests (REV-012): the approval matrix,
critical-blocker override policy, duplicate-request idempotency, the
second-approval hook, and rejection permissions."""

import uuid
from pathlib import Path
from typing import Any

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
AUDITOR = {"X-Dev-User": "user:auditor"}  # documents.read only: redacted payload view


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


_SEED_COUNTER = iter(range(100))


async def seed_reviewable(
    client: TestClient, db: DatabaseSessions, po_number: str | None = None
) -> str:
    """A document in REVIEW_REQUIRED with an OPEN task over a run whose
    po_number is missing by default (a BLOCKING rule) and whose header
    total disagrees with the single line (also blocking) — both
    correctable. Pass ``po_number`` to seed it present."""
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
            ("po_number", po_number, None),  # missing by default: blocking rule fires
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
    # PO number present so the sole blocker (the totals mismatch) is
    # overridable AND the canonical order remains buildable (CAN-003).
    task_id = await seed_reviewable(client, db, po_number="PO-77")
    claim(client, task_id, REVIEWER)

    # Blockers unresolved, no override requested: refused with the rule keys.
    refused = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve", json={}, headers=REVIEWER
    )
    assert refused.status_code == 409
    assert "totals.header_matches_lines" in refused.json()["error"]["message"]

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


async def canonical_payloads_for(
    db: DatabaseSessions, client: TestClient, task_id: str
) -> list[Any]:
    from soa_db.canonical_payloads import CanonicalPayload, CanonicalPayloadRepository

    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        task = await ReviewTaskRepository(session, context).get(uuid.UUID(task_id))
        assert task is not None
        repo = CanonicalPayloadRepository(session, context)
        stmt = repo._scoped_select().where(CanonicalPayload.run_id == task.run_id)
        return list((await session.execute(stmt)).scalars().all())


async def bind_approval_catalogs(client: TestClient, db: DatabaseSessions) -> None:
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
    async with db.session_scope() as session:
        for slug, catalog_type, source_id, name, aliases in (
            ("approval-products", "products", "WID-100", "Widget Master", ["Widget"]),
            ("approval-customers", "customers", "CUST-1", "Acme Master", ["Acme"]),
        ):
            catalog = await create_catalog(
                session,
                context,
                name=name,
                slug=slug,
                catalog_type=catalog_type,
                source="manual",
                actor_id="user:test",
            )
            version = await create_catalog_version(
                session, context, catalog=catalog, actor_id="user:test"
            )
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


def pick_catalog(
    client: TestClient,
    task_id: str,
    version: int,
    *,
    field_key: str,
    row_index: int | None,
    query: str,
    source_id: str,
) -> int:
    response = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/catalog-selection",
        json={
            "field_key": field_key,
            "row_index": row_index,
            "query": query,
            "selected_source_id": source_id,
            "expected_version": version,
        },
        headers=SUPERVISOR,
    )
    assert response.status_code == 200, response.text
    assert response.json()["selection"]["catalog_record_id"]
    return int(response.json()["task_version"])


async def test_approval_persists_the_immutable_canonical_payload(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    """CAN-003: approving builds and stores the canonical order exactly
    once per run, with provenance references; duplicates add nothing."""
    from soa_canonical import validate_order

    client, db = harness
    task_id = await seed_reviewable(client, db)
    version = claim(client, task_id, SUPERVISOR)
    version = correct(client, task_id, version, SUPERVISOR, field_key="po_number", value="PO-1")
    correct(client, task_id, version, SUPERVISOR, field_key="total_amount", value="450.00")

    approved = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve", json={}, headers=SUPERVISOR
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["canonical_payload_id"]

    (payload_row,) = await canonical_payloads_for(db, client, task_id)
    assert payload_row.schema_version == "1.0.0"
    payload = payload_row.payload
    validate_order(payload)
    assert payload["identifiers"]["po_number"] == "PO-1"
    assert payload["totals"]["grand_total"] == {"amount": "450.00", "currency": "USD"}
    assert payload["line_items"][0]["line_total"] == {"amount": "450.00", "currency": "USD"}
    # Provenance: the corrected field names its reviewer; extracted
    # fields carry their origin.
    assert payload["provenance"]["identifiers.po_number"]["origin"] == "corrected"
    assert payload["provenance"]["identifiers.po_number"]["actor"].startswith("user:")
    assert payload["provenance"]["dates.order_date"]["origin"] == "extracted"

    # A duplicate approval adds NOTHING (idempotent + immutable).
    duplicate = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve", json={}, headers=SUPERVISOR
    )
    assert duplicate.status_code == 200
    assert len(await canonical_payloads_for(db, client, task_id)) == 1


async def test_approval_revalidates_exact_catalog_ids_and_propagates_them(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    task_id = await seed_reviewable(client, db)
    await bind_approval_catalogs(client, db)
    version = claim(client, task_id, SUPERVISOR)
    version = correct(client, task_id, version, SUPERVISOR, field_key="po_number", value="PO-1")
    version = correct(
        client, task_id, version, SUPERVISOR, field_key="total_amount", value="450.00"
    )
    version = pick_catalog(
        client,
        task_id,
        version,
        field_key="lines.sku",
        row_index=0,
        query="WID-100",
        source_id="WID-100",
    )
    pick_catalog(
        client,
        task_id,
        version,
        field_key="customer_name",
        row_index=None,
        query="Acme",
        source_id="CUST-1",
    )

    approved = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve", json={}, headers=SUPERVISOR
    )
    assert approved.status_code == 200, approved.text
    (payload_row,) = await canonical_payloads_for(db, client, task_id)
    payload = payload_row.payload
    assert payload["parties"]["buyer"] == {
        "name": "Acme Master",
        "identifiers": [{"scheme": "customer-account", "value": "CUST-1"}],
    }
    assert payload["line_items"][0]["sku"] == "WID-100"
    extension = payload["extensions"]["x_soa_catalog"]
    assert uuid.UUID(extension["customer"]["catalog_record_id"])
    assert uuid.UUID(extension["line_items"][0]["catalog_version_id"])


async def test_catalog_version_change_blocks_canonicalization_even_with_override(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    from soa_db.catalogs import (
        CatalogRepository,
        activate_catalog_version,
        add_catalog_record,
        create_catalog_version,
    )

    client, db = harness
    task_id = await seed_reviewable(client, db)
    await bind_approval_catalogs(client, db)
    version = claim(client, task_id, SUPERVISOR)
    version = correct(client, task_id, version, SUPERVISOR, field_key="po_number", value="PO-1")
    version = correct(
        client, task_id, version, SUPERVISOR, field_key="total_amount", value="450.00"
    )
    version = pick_catalog(
        client,
        task_id,
        version,
        field_key="lines.sku",
        row_index=0,
        query="WID-100",
        source_id="WID-100",
    )
    pick_catalog(
        client,
        task_id,
        version,
        field_key="customer_name",
        row_index=None,
        query="Acme",
        source_id="CUST-1",
    )

    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        catalog = await CatalogRepository(session, context).get_by_slug("approval-products")
        assert catalog is not None
        replacement = await create_catalog_version(
            session, context, catalog=catalog, actor_id="user:test"
        )
        await add_catalog_record(
            session,
            context,
            version=replacement,
            source_id="WID-100",
            display_name="Widget Master rev B",
        )
        await activate_catalog_version(
            session,
            context,
            catalog=catalog,
            version=replacement,
            actor_id="user:test",
        )

    blocked = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve",
        json={"override_reason": "attempting an authorized rule override"},
        headers=SUPERVISOR,
    )
    assert blocked.status_code == 409
    assert "catalog version changed" in blocked.json()["error"]["message"]
    assert await canonical_payloads_for(db, client, task_id) == []


async def test_mapping_errors_block_approval_clearly(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    """CAN-003: a payload that cannot be built refuses the approval with
    every problem named — even an authorized override cannot approve a
    document whose canonical order would be incomplete."""
    client, db = harness
    task_id = await seed_reviewable(client, db)  # po_number missing
    claim(client, task_id, SUPERVISOR)

    blocked = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve",
        json={"override_reason": "pushing it through anyway"},
        headers=SUPERVISOR,
    )
    assert blocked.status_code == 409
    message = blocked.json()["error"]["message"]
    assert "canonical order cannot be built" in message
    assert "identifiers.po_number" in message

    # Nothing was committed: the task is still in progress and OWNED,
    # no payload exists, and fixing the field unblocks the approval.
    still_mine = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/workspace", headers=SUPERVISOR
    ).json()["task"]
    assert still_mine["state"] == "in_progress"
    assert await canonical_payloads_for(db, client, task_id) == []

    fixed = correct(
        client, task_id, still_mine["version"], SUPERVISOR, field_key="po_number", value="PO-9"
    )
    approved = client.post(
        f"/orgs/northstar/review-tasks/{task_id}/approve",
        json={"override_reason": "totals confirmed by phone"},
        headers=SUPERVISOR,
    )
    assert approved.status_code == 200, approved.text
    assert fixed > 0
    (payload_row,) = await canonical_payloads_for(db, client, task_id)
    assert payload_row.payload["identifiers"]["po_number"] == "PO-9"


async def test_canonical_payload_endpoint_permissions_and_redaction(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    """CAN-004: reviewers get the full payload and may copy/download;
    read-only callers get a SERVER-redacted projection (internal notes
    and reviewer identities never leave the server)."""
    from soa_db.canonical_payloads import record_canonical_payload

    client, db = harness
    task_id = await seed_reviewable(client, db)
    workspace = client.get(
        f"/orgs/northstar/review-tasks/{task_id}/workspace", headers=SUPERVISOR
    ).json()
    document_id = workspace["document"]["id"]
    payload_url = f"/orgs/northstar/documents/{document_id}/canonical-payload"

    # Before approval there is nothing — an honest 404, not an empty shell.
    missing = client.get(payload_url, headers=SUPERVISOR)
    assert missing.status_code == 404
    assert "created when the document is approved" in missing.json()["error"]["message"]

    version = claim(client, task_id, SUPERVISOR)
    version = correct(client, task_id, version, SUPERVISOR, field_key="po_number", value="PO-1")
    correct(client, task_id, version, SUPERVISOR, field_key="total_amount", value="450.00")
    assert (
        client.post(
            f"/orgs/northstar/review-tasks/{task_id}/approve", json={}, headers=SUPERVISOR
        ).status_code
        == 200
    )

    full = client.get(payload_url, headers=SUPERVISOR).json()
    assert full["can_copy"] is True
    assert full["redacted"] is False
    assert full["created_by"].startswith("user:")
    assert full["payload"]["provenance"]["identifiers.po_number"]["actor"].startswith("user:")

    read_only = client.get(payload_url, headers=AUDITOR).json()
    assert read_only["can_copy"] is False
    assert read_only["redacted"] is True
    assert read_only["created_by"] is None
    assert read_only["sha256"] == full["sha256"]  # same payload, provably
    for entry in read_only["payload"]["provenance"].values():
        assert "actor" not in entry
    # Business content is intact.
    assert read_only["payload"]["identifiers"]["po_number"] == "PO-1"

    # Internal notes are stripped server-side for read-only callers.
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    noted_run = uuid.uuid4()
    async with db.session_scope() as session:
        await record_canonical_payload(
            session,
            context,
            document_id=uuid.UUID(document_id),
            run_id=noted_run,
            task_id=None,
            schema_version="1.0.0",
            payload={
                "schema_version": "1.0.0",
                "identifiers": {"po_number": "PO-1"},
                "notes": [
                    {
                        "text": "internal margin talk",
                        "author": "user:u-1",
                        "visibility": "internal",
                    },
                    {"text": "liftgate requested", "author": None, "visibility": "external"},
                ],
                "line_items": [
                    {
                        "line_number": 1,
                        "quantity": "1",
                        "line_total": {"amount": "1", "currency": "USD"},
                        "notes": [{"text": "swap to rev B", "visibility": "internal"}],
                    }
                ],
            },
            actor_id="user:test",
        )
    noted = client.get(f"{payload_url}?run_id={noted_run}", headers=AUDITOR).json()
    assert [n["text"] for n in noted["payload"]["notes"]] == ["liftgate requested"]
    assert noted["payload"]["line_items"][0]["notes"] == []
    assert "internal margin talk" not in str(noted)
    supervisor_noted = client.get(f"{payload_url}?run_id={noted_run}", headers=SUPERVISOR).json()
    assert len(supervisor_noted["payload"]["notes"]) == 2


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
