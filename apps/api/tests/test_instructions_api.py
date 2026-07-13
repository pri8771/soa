"""Instruction-version API tests (AIO-010): permissioned content
access (its own grants, not streams.*), the draft -> publish ->
supersede lifecycle over HTTP, and the exact-version reference."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine

ADMIN = {"X-Dev-User": "user:admin"}  # org creator -> org-admin
SUPERVISOR = {"X-Dev-User": "user:supervisor"}  # instructions.read only
REVIEWER = {"X-Dev-User": "user:reviewer"}  # no instructions permissions

SCHEMA = {"fields": [{"key": "po_number", "label": "PO", "type": "text", "required": True}]}
CONTENT = {
    "instructions": "Extract the fields verbatim; use null when a value is absent.",
    "field_guidance": {"po_number": "top right, labelled 'PO No.'"},
}


@pytest.fixture
async def client(tmp_path: Path) -> TestClient:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/instructions-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(ApiSettings(environment=Environment.TEST), db=DatabaseSessions(engine))
    test_client = TestClient(app, raise_server_exceptions=False)
    assert (
        test_client.post(
            "/organizations", json={"name": "Northstar", "slug": "northstar"}, headers=ADMIN
        ).status_code
        == 201
    )
    for headers, email, role in (
        (SUPERVISOR, "supervisor@northstar.example", "supervisor"),
        (REVIEWER, "reviewer@northstar.example", "reviewer"),
    ):
        test_client.post("/orgs/northstar/invitations", json={"email": email}, headers=ADMIN)
        accepted = test_client.post(
            "/invitations/accept", json={"organization_slug": "northstar"}, headers=headers
        )
        granted = test_client.post(
            f"/orgs/northstar/members/{accepted.json()['membership_id']}/roles",
            json={"role_slug": role},
            headers=ADMIN,
        )
        assert granted.status_code == 201, granted.text
    return test_client


def seed_config(client: TestClient) -> tuple[str, str]:
    """Create a process, stream, stream-version draft and schema draft
    via the API; returns (stream_version_id, schema_version_id)."""
    assert (
        client.post(
            "/orgs/northstar/processes",
            json={"name": "Purchase orders", "slug": "purchase-orders"},
            headers=ADMIN,
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/orgs/northstar/processes/purchase-orders/streams",
            json={"name": "Email intake", "slug": "email"},
            headers=ADMIN,
        ).status_code
        == 201
    )
    stream_version = client.post("/orgs/northstar/streams/email/versions", json={}, headers=ADMIN)
    assert stream_version.status_code == 201, stream_version.text
    schema_version = client.post(
        "/orgs/northstar/processes/purchase-orders/schema/versions",
        json={"definition": SCHEMA},
        headers=ADMIN,
    )
    assert schema_version.status_code == 201, schema_version.text
    return stream_version.json()["id"], schema_version.json()["id"]


def make_draft(client: TestClient, stream_version_id: str, schema_version_id: str) -> dict:
    created = client.post(
        f"/orgs/northstar/stream-versions/{stream_version_id}/instructions",
        json={"schema_version_id": schema_version_id, "content": CONTENT},
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_lifecycle_draft_publish_supersede_with_exact_reference(client: TestClient) -> None:
    stream_version_id, schema_version_id = seed_config(client)
    first = make_draft(client, stream_version_id, schema_version_id)
    assert first["version_number"] == 1
    assert first["state"] == "draft"
    assert first["reference"] == f"instruction:{first['id']}:v1"

    published = client.post(f"/orgs/northstar/instructions/{first['id']}/publish", headers=ADMIN)
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"

    second = make_draft(client, stream_version_id, schema_version_id)
    assert second["version_number"] == 2
    assert (
        client.post(
            f"/orgs/northstar/instructions/{second['id']}/publish", headers=ADMIN
        ).status_code
        == 200
    )

    listed = client.get(
        f"/orgs/northstar/stream-versions/{stream_version_id}/instructions", headers=ADMIN
    ).json()["items"]
    states = {item["version_number"]: item["state"] for item in listed}
    assert states == {1: "superseded", 2: "published"}


def test_published_versions_refuse_edits_with_a_conflict(client: TestClient) -> None:
    stream_version_id, schema_version_id = seed_config(client)
    draft = make_draft(client, stream_version_id, schema_version_id)
    client.post(f"/orgs/northstar/instructions/{draft['id']}/publish", headers=ADMIN)
    refused = client.patch(
        f"/orgs/northstar/instructions/{draft['id']}",
        json={"content": {"instructions": "rewritten history"}},
        headers=ADMIN,
    )
    assert refused.status_code == 409
    again = client.post(f"/orgs/northstar/instructions/{draft['id']}/publish", headers=ADMIN)
    assert again.status_code == 409


def test_drafts_are_editable(client: TestClient) -> None:
    stream_version_id, schema_version_id = seed_config(client)
    draft = make_draft(client, stream_version_id, schema_version_id)
    updated = client.patch(
        f"/orgs/northstar/instructions/{draft['id']}",
        json={
            "content": {"instructions": "Extract carefully.", "field_guidance": {}},
            "change_summary": "simplified",
        },
        headers=ADMIN,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["content"]["instructions"] == "Extract carefully."


def test_content_access_is_permissioned_separately_from_streams(client: TestClient) -> None:
    stream_version_id, schema_version_id = seed_config(client)
    draft = make_draft(client, stream_version_id, schema_version_id)

    # The supervisor holds instructions.read: content visible, writes refused.
    read = client.get(f"/orgs/northstar/instructions/{draft['id']}", headers=SUPERVISOR)
    assert read.status_code == 200
    assert read.json()["content"]["instructions"].startswith("Extract")
    refused_write = client.post(
        f"/orgs/northstar/instructions/{draft['id']}/publish", headers=SUPERVISOR
    )
    assert refused_write.status_code == 403

    # The reviewer holds streams.read but NOT instructions.read: prompt
    # content is invisible to them.
    refused_read = client.get(f"/orgs/northstar/instructions/{draft['id']}", headers=REVIEWER)
    assert refused_read.status_code == 403
    refused_list = client.get(
        f"/orgs/northstar/stream-versions/{stream_version_id}/instructions", headers=REVIEWER
    )
    assert refused_list.status_code == 403


def test_invalid_content_is_rejected_with_the_reason(client: TestClient) -> None:
    stream_version_id, schema_version_id = seed_config(client)
    refused = client.post(
        f"/orgs/northstar/stream-versions/{stream_version_id}/instructions",
        json={"schema_version_id": schema_version_id, "content": {"instructions": "   "}},
        headers=ADMIN,
    )
    assert refused.status_code == 422


def test_unknown_stream_or_schema_versions_are_404(client: TestClient) -> None:
    stream_version_id, schema_version_id = seed_config(client)
    ghost = "00000000-0000-4000-8000-000000000000"
    assert (
        client.post(
            f"/orgs/northstar/stream-versions/{ghost}/instructions",
            json={"schema_version_id": schema_version_id, "content": CONTENT},
            headers=ADMIN,
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/orgs/northstar/stream-versions/{stream_version_id}/instructions",
            json={"schema_version_id": ghost, "content": CONTENT},
            headers=ADMIN,
        ).status_code
        == 404
    )
