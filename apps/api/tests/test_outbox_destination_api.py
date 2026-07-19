"""Per-tenant outbox destination admin API tests (EXP-012)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:admin"}  # org creator -> org-admin
REVIEWER = {"X-Dev-User": "user:reviewer"}  # invited, no integrations.manage


@pytest.fixture(autouse=True)
def _public_dns_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    # hooks.northstar.example is not a real, resolvable host; stand in a
    # public address so allowlisted https:// URLs pass SSRF validation the
    # same way test_integrations_api.py does for its own .example hosts.
    monkeypatch.setattr(
        "soa_integrations.destination._default_resolver",
        lambda _host: ["93.184.216.34"],
    )


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/outbox-destination-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(
        ApiSettings(
            environment=Environment.TEST,
            outbound_destination_allowlist=("hooks.northstar.example",),
        ),
        db=db,
        object_store=MemoryObjectStore(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    assert (
        client.post(
            "/organizations", json={"name": "Northstar", "slug": "northstar"}, headers=ADMIN
        ).status_code
        == 201
    )
    client.post(
        "/orgs/northstar/invitations", json={"email": "reviewer@northstar.example"}, headers=ADMIN
    )
    accepted = client.post(
        "/invitations/accept", json={"organization_slug": "northstar"}, headers=REVIEWER
    )
    granted = client.post(
        f"/orgs/northstar/members/{accepted.json()['membership_id']}/roles",
        json={"role_slug": "reviewer"},
        headers=ADMIN,
    )
    assert granted.status_code == 201, granted.text
    return client, db


def create_second_org(client: TestClient) -> None:
    assert (
        client.post(
            "/organizations", json={"name": "Acme", "slug": "acme"}, headers=ADMIN
        ).status_code
        == 201
    )


async def test_get_is_null_when_unconfigured(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    response = client.get("/orgs/northstar/outbox-destination", headers=ADMIN)
    assert response.status_code == 200
    assert response.json() is None


async def test_admin_can_set_get_and_delete(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    created = client.put(
        "/orgs/northstar/outbox-destination",
        json={"destination_url": "https://hooks.northstar.example/events"},
        headers=ADMIN,
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["destination_url"] == "https://hooks.northstar.example/events"
    assert body["is_active"] is True
    assert body["version"] == 1

    fetched = client.get("/orgs/northstar/outbox-destination", headers=ADMIN)
    assert fetched.status_code == 200
    assert fetched.json()["destination_url"] == "https://hooks.northstar.example/events"

    deleted = client.delete(
        "/orgs/northstar/outbox-destination",
        headers={**ADMIN, "If-Match": "1"},
    )
    assert deleted.status_code == 204

    assert client.get("/orgs/northstar/outbox-destination", headers=ADMIN).json() is None


async def test_update_requires_a_matching_if_match(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    client.put(
        "/orgs/northstar/outbox-destination",
        json={"destination_url": "https://hooks.northstar.example/events"},
        headers=ADMIN,
    )
    stale = client.put(
        "/orgs/northstar/outbox-destination",
        json={"destination_url": "https://hooks.northstar.example/other"},
        headers={**ADMIN, "If-Match": "99"},
    )
    assert stale.status_code == 409


async def test_delete_without_a_configured_destination_is_404(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    response = client.delete("/orgs/northstar/outbox-destination", headers=ADMIN)
    assert response.status_code == 404


async def test_non_admin_cannot_manage_the_destination(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    assert client.get("/orgs/northstar/outbox-destination", headers=REVIEWER).status_code == 403
    assert (
        client.put(
            "/orgs/northstar/outbox-destination",
            json={"destination_url": "https://hooks.northstar.example/events"},
            headers=REVIEWER,
        ).status_code
        == 403
    )
    assert client.delete("/orgs/northstar/outbox-destination", headers=REVIEWER).status_code == 403


@pytest.mark.parametrize(
    "destination_url",
    [
        "http://hooks.northstar.example/events",  # not https
        "https://169.254.169.254/events",  # link-local metadata address literal, not a host
        "https://evil.example/events",  # not on the allowlist
    ],
)
async def test_invalid_urls_are_rejected(
    harness: tuple[TestClient, DatabaseSessions], destination_url: str
) -> None:
    client, _ = harness
    response = client.put(
        "/orgs/northstar/outbox-destination",
        json={"destination_url": destination_url},
        headers=ADMIN,
    )
    assert response.status_code == 422


async def test_cross_tenant_destination_is_invisible(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    client.put(
        "/orgs/northstar/outbox-destination",
        json={"destination_url": "https://hooks.northstar.example/events"},
        headers=ADMIN,
    )
    create_second_org(client)
    other_org_admin = {"X-Dev-User": "user:admin"}  # same principal, different org membership
    response = client.get("/orgs/acme/outbox-destination", headers=other_org_admin)
    assert response.status_code == 200
    assert response.json() is None


async def test_put_writes_an_audit_event(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    client.put(
        "/orgs/northstar/outbox-destination",
        json={"destination_url": "https://hooks.northstar.example/events"},
        headers=ADMIN,
    )
    async with db.session_scope() as session:
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "outbox_destination.created")
            )
        ).scalar_one()
        assert event.target_type == "outbox_destination"


async def test_delete_writes_an_audit_event(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    client.put(
        "/orgs/northstar/outbox-destination",
        json={"destination_url": "https://hooks.northstar.example/events"},
        headers=ADMIN,
    )
    client.delete("/orgs/northstar/outbox-destination", headers={**ADMIN, "If-Match": "1"})
    async with db.session_scope() as session:
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "outbox_destination.deleted")
            )
        ).scalar_one()
        assert event.target_type == "outbox_destination"
