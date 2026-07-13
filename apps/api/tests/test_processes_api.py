"""Process/stream API integration tests (CFG-008): lifecycle through HTTP,
optimistic concurrency, permission checks, cross-tenant invisibility."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.domain.policies import PolicyType, create_policy_draft, publish_policy_draft
from soa_api.domain.schemas import create_schema_draft, publish_schema_draft
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext

ADMIN = {"X-Dev-User": "user:reviewer"}  # org creator -> org-admin
OUTSIDER = {"X-Dev-User": "user:supervisor"}  # no membership

SCHEMA = {"fields": [{"key": "po_number", "label": "PO", "type": "text", "required": True}]}
PROVIDER = {"provider_name": "acme", "capabilities": ["ocr", "field_extraction"]}


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/processes-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(ApiSettings(environment=Environment.TEST), db=db)
    return TestClient(app, raise_server_exceptions=False), db


@pytest.fixture
def client(harness: tuple[TestClient, DatabaseSessions]) -> TestClient:
    return harness[0]


async def seed_publish_prereqs(client: TestClient, db: DatabaseSessions) -> None:
    """Publish a schema + provider policy through the domain layer (their
    HTTP surfaces arrive with CFG-011/012) so process publish can succeed."""
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        from soa_api.domain.processes import ProcessRepository

        process = await ProcessRepository(session, context).get_by_slug("purchase-orders")
        assert process is not None
        schema = await create_schema_draft(
            session, context, process_id=process.id, definition=SCHEMA, actor_id="user:test"
        )
        await publish_schema_draft(session, context, draft=schema, actor_id="user:test")
        policy = await create_policy_draft(
            session,
            context,
            policy_type=PolicyType.PROVIDER,
            definition=PROVIDER,
            actor_id="user:test",
        )
        await publish_policy_draft(session, context, draft=policy, actor_id="user:test")


def make_org(client: TestClient) -> None:
    response = client.post(
        "/organizations", json={"name": "Northstar", "slug": "northstar"}, headers=ADMIN
    )
    assert response.status_code == 201


def make_process(client: TestClient, slug: str = "purchase-orders") -> None:
    response = client.post(
        "/orgs/northstar/processes",
        json={"name": "Purchase orders", "slug": slug},
        headers=ADMIN,
    )
    assert response.status_code == 201, response.text


def test_create_list_detail_and_cross_tenant_invisibility(client: TestClient) -> None:
    make_org(client)
    make_process(client)

    listed = client.get("/orgs/northstar/processes", headers=ADMIN)
    assert listed.status_code == 200
    assert [p["slug"] for p in listed.json()] == ["purchase-orders"]

    detail = client.get("/orgs/northstar/processes/purchase-orders", headers=ADMIN)
    assert detail.status_code == 200
    assert detail.json()["process"]["status"] == "active"
    assert detail.json()["versions"] == []

    # Outsider: organization existence must not leak.
    assert client.get("/orgs/northstar/processes", headers=OUTSIDER).status_code == 404

    duplicate = client.post(
        "/orgs/northstar/processes",
        json={"name": "Again", "slug": "purchase-orders"},
        headers=ADMIN,
    )
    assert duplicate.status_code == 409


def test_draft_lifecycle_with_optimistic_concurrency_and_clone(client: TestClient) -> None:
    make_org(client)
    make_process(client)

    draft = client.post(
        "/orgs/northstar/processes/purchase-orders/versions",
        json={"definition": {"language": "en"}, "change_summary": "initial"},
        headers=ADMIN,
    )
    assert draft.status_code == 201, draft.text
    body = draft.json()
    assert body["version_number"] == 1 and body["state"] == "draft"
    version_id = body["id"]

    # Stale If-Match is rejected; the fresh one succeeds.
    stale = client.patch(
        f"/orgs/northstar/processes/purchase-orders/versions/{version_id}",
        json={"definition": {"language": "de"}},
        headers={**ADMIN, "If-Match": "41"},
    )
    assert stale.status_code == 409
    updated = client.patch(
        f"/orgs/northstar/processes/purchase-orders/versions/{version_id}",
        json={"definition": {"language": "de"}},
        headers={**ADMIN, "If-Match": str(body["version"])},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["definition"] == {"language": "de"}

    # Clone seeds the next draft from an existing version.
    cloned = client.post(
        "/orgs/northstar/processes/purchase-orders/versions",
        json={"from_version_id": version_id},
        headers=ADMIN,
    )
    assert cloned.status_code == 201
    assert cloned.json()["version_number"] == 2
    assert cloned.json()["definition"] == {"language": "de"}


def test_validate_and_publish_report_flow(client: TestClient) -> None:
    make_org(client)
    make_process(client)
    draft = client.post(
        "/orgs/northstar/processes/purchase-orders/versions", json={}, headers=ADMIN
    ).json()

    # No provider policy published -> validation reports the blocker and
    # publish refuses with the same report.
    report = client.post(
        f"/orgs/northstar/processes/purchase-orders/versions/{draft['id']}/validate",
        headers=ADMIN,
    )
    assert report.status_code == 200
    assert report.json()["publishable"] is False
    refused = client.post(
        f"/orgs/northstar/processes/purchase-orders/versions/{draft['id']}/publish",
        headers=ADMIN,
    )
    assert refused.status_code == 422
    assert "provider" in refused.text, "the refusal must carry the validation report"

    # Draft remains editable after the refused publish.
    still_draft = client.get(
        f"/orgs/northstar/processes/purchase-orders/versions/{draft['id']}", headers=ADMIN
    )
    assert still_draft.json()["state"] == "draft"


async def test_publish_and_rollback_through_the_api(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    make_org(client)
    make_process(client)
    await seed_publish_prereqs(client, db)

    first = client.post(
        "/orgs/northstar/processes/purchase-orders/versions", json={}, headers=ADMIN
    ).json()
    published = client.post(
        f"/orgs/northstar/processes/purchase-orders/versions/{first['id']}/publish",
        headers=ADMIN,
    )
    assert published.status_code == 200, published.text
    assert published.json()["version"]["state"] == "published"
    assert published.json()["report"]["publishable"] is True

    second = client.post(
        "/orgs/northstar/processes/purchase-orders/versions", json={}, headers=ADMIN
    ).json()
    assert (
        client.post(
            f"/orgs/northstar/processes/purchase-orders/versions/{second['id']}/publish",
            headers=ADMIN,
        ).status_code
        == 200
    )
    detail = client.get("/orgs/northstar/processes/purchase-orders", headers=ADMIN).json()
    assert detail["process"]["active_version_id"] == second["id"]
    states = {v["id"]: v["state"] for v in detail["versions"]}
    assert states[first["id"]] == "superseded"

    # Rollback moves the pointer back to the superseded version; a draft
    # target is refused.
    rolled = client.post(
        "/orgs/northstar/processes/purchase-orders/rollback",
        json={"target_version_id": first["id"], "reason": "bad mapping in v2"},
        headers=ADMIN,
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["active_version_id"] == first["id"]

    draft = client.post(
        "/orgs/northstar/processes/purchase-orders/versions", json={}, headers=ADMIN
    ).json()
    refused = client.post(
        "/orgs/northstar/processes/purchase-orders/rollback",
        json={"target_version_id": draft["id"], "reason": "should fail"},
        headers=ADMIN,
    )
    assert refused.status_code == 409


def test_streams_over_http_pin_the_active_process_version(client: TestClient) -> None:
    make_org(client)
    make_process(client)
    stream = client.post(
        "/orgs/northstar/processes/purchase-orders/streams",
        json={"name": "Email intake", "slug": "email"},
        headers=ADMIN,
    )
    assert stream.status_code == 201, stream.text

    draft = client.post(
        "/orgs/northstar/streams/email/versions",
        json={"overrides": {"confidence_floor": 0.95}},
        headers=ADMIN,
    )
    assert draft.status_code == 201

    # Publishing a stream before the process has a published version is a
    # clear conflict, not a mystery 500.
    blocked = client.post(
        f"/orgs/northstar/streams/email/versions/{draft.json()['id']}/publish",
        headers=ADMIN,
    )
    assert blocked.status_code == 409
    assert "no published version" in blocked.json()["error"]["message"]

    detail = client.get("/orgs/northstar/streams/email", headers=ADMIN)
    assert detail.status_code == 200
    assert detail.json()["versions"][0]["state"] == "draft"


def test_reads_require_read_permission_and_writes_require_manage(client: TestClient) -> None:
    make_org(client)
    make_process(client)
    # Invite a member and give them auditor (processes.read, no manage).
    client.post(
        "/orgs/northstar/invitations",
        json={"email": "supervisor@northstar.example"},
        headers=ADMIN,
    )
    accepted = client.post(
        "/invitations/accept", json={"organization_slug": "northstar"}, headers=OUTSIDER
    )
    membership_id = accepted.json()["membership_id"]
    assert (
        client.post(
            f"/orgs/northstar/members/{membership_id}/roles",
            json={"role_slug": "auditor"},
            headers=ADMIN,
        ).status_code
        == 201
    )

    assert client.get("/orgs/northstar/processes", headers=OUTSIDER).status_code == 200
    denied = client.post(
        "/orgs/northstar/processes",
        json={"name": "Nope", "slug": "nope"},
        headers=OUTSIDER,
    )
    assert denied.status_code == 403
