"""Provider administration API tests (AIO-019): the catalog with
explicit data-policy warnings, the honest unknown health, the approved
provider's credential REFERENCE (never a secret), and the routing
preview."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.domain.policies import PolicyType, create_policy_draft, publish_policy_draft
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext

ADMIN = {"X-Dev-User": "user:admin"}
OUTSIDER = {"X-Dev-User": "user:supervisor"}  # no membership


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/providers-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(ApiSettings(environment=Environment.TEST), db=db)
    client = TestClient(app, raise_server_exceptions=False)
    assert (
        client.post(
            "/organizations", json={"name": "Northstar", "slug": "northstar"}, headers=ADMIN
        ).status_code
        == 201
    )
    return client, db


async def approve_provider(client: TestClient, db: DatabaseSessions, name: str) -> None:
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        draft = await create_policy_draft(
            session,
            context,
            policy_type=PolicyType.PROVIDER,
            definition={
                "provider_name": name,
                "capabilities": ["ocr", "field_extraction"],
                "credential_ref": "credential:main",
            },
            actor_id="user:test",
        )
        await publish_policy_draft(session, context, draft=draft, actor_id="user:test")


async def test_the_catalog_is_served_with_explicit_warnings_and_honest_health(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    response = client.get("/orgs/northstar/providers", headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()
    by_name = {item["name"]: item for item in body["items"]}
    assert {"mock", "pdfium-native-text", "tesseract", "local-openai-compatible"} <= set(by_name)
    native = by_name["pdfium-native-text"]
    assert native["local"] is True
    assert native["health"] == "unknown"
    assert any("local-only safe" in warning for warning in native["warnings"])
    assert "reported by the worker at runtime" in body["health_note"]
    # Nobody is approved and no credential reference exists yet.
    assert all(item["approved"] is False for item in body["items"])
    assert all(item["credential_ref"] is None for item in body["items"])


async def test_the_approved_provider_carries_only_the_credential_reference(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    await approve_provider(client, db, "tesseract")
    response = client.get("/orgs/northstar/providers", headers=ADMIN)
    by_name = {item["name"]: item for item in response.json()["items"]}
    assert by_name["tesseract"]["approved"] is True
    assert by_name["tesseract"]["credential_ref"] == "credential:main"
    # The REFERENCE is the only credential-shaped thing on this surface.
    assert "secret" not in response.text.lower()


async def test_routing_preview_honours_local_only(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    preview = client.post(
        "/orgs/northstar/providers/routing-preview",
        json={"capability": "field_extraction", "local_only": True},
        headers=ADMIN,
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert "mock" in body["order"]
    assert any("preview order" in line for line in body["explanation"])
    refused = client.post(
        "/orgs/northstar/providers/routing-preview",
        json={"capability": "mind_reading", "local_only": False},
        headers=ADMIN,
    )
    assert refused.status_code == 422


async def test_non_members_see_nothing(harness: tuple[TestClient, DatabaseSessions]) -> None:
    client, _ = harness
    assert client.get("/orgs/northstar/providers", headers=OUTSIDER).status_code == 404
