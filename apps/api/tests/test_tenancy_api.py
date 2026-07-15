"""API integration tests for TEN-008 (organizations, members, invitations,
roles) driven through dev-identity principals."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine

REVIEWER = {"X-Dev-User": "user:reviewer"}
SUPERVISOR = {"X-Dev-User": "user:supervisor"}
AUDITOR = {"X-Dev-User": "user:auditor"}


@pytest.fixture
async def client(tmp_path: Path) -> TestClient:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(ApiSettings(environment=Environment.TEST), db=db)
    return TestClient(app, raise_server_exceptions=False)


def create_org(client: TestClient, slug: str = "northstar", headers: dict[str, str] = REVIEWER):
    return client.post("/organizations", json={"name": "Northstar", "slug": slug}, headers=headers)


def test_me_provisions_user_and_lists_memberships(client: TestClient) -> None:
    response = client.get("/me", headers=REVIEWER)
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "reviewer@northstar.example"
    assert body["dev_session"] is True
    assert body["memberships"] == []

    assert create_org(client).status_code == 201
    body = client.get("/me", headers=REVIEWER).json()
    assert len(body["memberships"]) == 1
    assert body["memberships"][0]["status"] == "active"


def test_create_organization_makes_creator_admin(client: TestClient) -> None:
    response = create_org(client)
    assert response.status_code == 201
    body = response.json()
    assert body["slug"] == "northstar"
    assert body["status"] == "active"

    # Creator holds org-admin -> can read org, list members, manage roles.
    assert client.get("/orgs/northstar", headers=REVIEWER).status_code == 200
    members = client.get("/orgs/northstar/members", headers=REVIEWER)
    assert members.status_code == 200
    assert len(members.json()["items"]) == 1

    roles = client.get("/orgs/northstar/roles", headers=REVIEWER)
    assert roles.status_code == 200
    assert {role["slug"] for role in roles.json()} >= {"org-admin", "reviewer", "auditor"}


def test_duplicate_slug_conflicts(client: TestClient) -> None:
    assert create_org(client).status_code == 201
    duplicate = create_org(client, headers=SUPERVISOR)
    assert duplicate.status_code == 409


def test_non_member_cannot_see_organization(client: TestClient) -> None:
    assert create_org(client).status_code == 201
    response = client.get("/orgs/northstar", headers=SUPERVISOR)
    assert response.status_code == 404, "membership existence must not leak"


def test_invitation_flow_with_safe_duplicates(client: TestClient) -> None:
    assert create_org(client).status_code == 201

    invite = client.post(
        "/orgs/northstar/invitations",
        json={"email": "Supervisor@Northstar.Example"},
        headers=REVIEWER,
    )
    assert invite.status_code == 201
    first = invite.json()
    assert first["created"] is True
    assert first["email"] == "supervisor@northstar.example"

    again = client.post(
        "/orgs/northstar/invitations",
        json={"email": "supervisor@northstar.example"},
        headers=REVIEWER,
    )
    assert again.status_code == 201
    assert again.json()["created"] is False
    assert again.json()["membership_id"] == first["membership_id"]

    accepted = client.post(
        "/invitations/accept", json={"organization_slug": "northstar"}, headers=SUPERVISOR
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "active"

    # New member has no roles yet -> cannot read members.
    assert client.get("/orgs/northstar/members", headers=SUPERVISOR).status_code == 403


def test_accept_without_invitation_is_404(client: TestClient) -> None:
    assert create_org(client).status_code == 201
    response = client.post(
        "/invitations/accept", json={"organization_slug": "northstar"}, headers=AUDITOR
    )
    assert response.status_code == 404


def test_role_assignment_grants_permissions(client: TestClient) -> None:
    assert create_org(client).status_code == 201
    client.post(
        "/orgs/northstar/invitations",
        json={"email": "supervisor@northstar.example"},
        headers=REVIEWER,
    )
    accepted = client.post(
        "/invitations/accept", json={"organization_slug": "northstar"}, headers=SUPERVISOR
    )
    membership_id = accepted.json()["membership_id"]

    assign = client.post(
        f"/orgs/northstar/members/{membership_id}/roles",
        json={"role_slug": "supervisor"},
        headers=REVIEWER,
    )
    assert assign.status_code == 201

    assigned = client.get(f"/orgs/northstar/members/{membership_id}/roles", headers=REVIEWER)
    assert assigned.status_code == 200
    assert [role["slug"] for role in assigned.json()] == ["supervisor"]
    listed_member = next(
        member
        for member in client.get("/orgs/northstar/members", headers=REVIEWER).json()["items"]
        if member["membership_id"] == membership_id
    )
    assert [role["slug"] for role in listed_member["assigned_roles"]] == ["supervisor"]

    # supervisor template includes members.read
    assert client.get("/orgs/northstar/members", headers=SUPERVISOR).status_code == 200

    revoke = client.delete(
        f"/orgs/northstar/members/{membership_id}/roles/supervisor", headers=REVIEWER
    )
    assert revoke.status_code == 200
    assert revoke.json()["revoked"] is True
    assert (
        client.get(f"/orgs/northstar/members/{membership_id}/roles", headers=REVIEWER).json() == []
    )
    assert client.get("/orgs/northstar/members", headers=SUPERVISOR).status_code == 403


def test_last_active_org_admin_cannot_orphan_the_organization(client: TestClient) -> None:
    assert create_org(client).status_code == 201
    creator = client.get("/orgs/northstar/members", headers=REVIEWER).json()["items"][0]

    revoke_last = client.delete(
        f"/orgs/northstar/members/{creator['membership_id']}/roles/org-admin",
        headers=REVIEWER,
    )
    assert revoke_last.status_code == 409
    assert "At least one active organization admin" in revoke_last.text
    suspend_last = client.patch(
        f"/orgs/northstar/members/{creator['membership_id']}",
        json={"status": "suspended"},
        headers={**REVIEWER, "If-Match": str(creator["version"])},
    )
    assert suspend_last.status_code == 409

    client.post(
        "/orgs/northstar/invitations",
        json={"email": "supervisor@northstar.example"},
        headers=REVIEWER,
    )
    second = client.post(
        "/invitations/accept", json={"organization_slug": "northstar"}, headers=SUPERVISOR
    ).json()
    assert (
        client.post(
            f"/orgs/northstar/members/{second['membership_id']}/roles",
            json={"role_slug": "org-admin"},
            headers=REVIEWER,
        ).status_code
        == 201
    )

    # A self-suspension is allowed only because another active admin exists.
    suspended = client.patch(
        f"/orgs/northstar/members/{creator['membership_id']}",
        json={"status": "suspended"},
        headers={**REVIEWER, "If-Match": str(creator["version"])},
    )
    assert suspended.status_code == 200, suspended.text

    # The remaining admin still cannot remove their own final grant/access.
    assert (
        client.delete(
            f"/orgs/northstar/members/{second['membership_id']}/roles/org-admin",
            headers=SUPERVISOR,
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/orgs/northstar/members/{second['membership_id']}",
            json={"status": "removed"},
            headers={**SUPERVISOR, "If-Match": str(second["version"])},
        ).status_code
        == 409
    )


def test_membership_status_change_with_optimistic_concurrency(client: TestClient) -> None:
    assert create_org(client).status_code == 201
    client.post(
        "/orgs/northstar/invitations",
        json={"email": "supervisor@northstar.example"},
        headers=REVIEWER,
    )
    accepted = client.post(
        "/invitations/accept", json={"organization_slug": "northstar"}, headers=SUPERVISOR
    )
    membership_id = accepted.json()["membership_id"]
    version = accepted.json()["version"]

    stale = client.patch(
        f"/orgs/northstar/members/{membership_id}",
        json={"status": "suspended"},
        headers={**REVIEWER, "If-Match": str(version + 5)},
    )
    assert stale.status_code == 409

    ok = client.patch(
        f"/orgs/northstar/members/{membership_id}",
        json={"status": "suspended"},
        headers={**REVIEWER, "If-Match": str(version)},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "suspended"

    illegal = client.patch(
        f"/orgs/northstar/members/{membership_id}",
        json={"status": "invited"},
        headers=REVIEWER,
    )
    assert illegal.status_code == 409, "invalid transition must conflict"


def test_custom_role_with_unknown_permission_is_422(client: TestClient) -> None:
    assert create_org(client).status_code == 201
    response = client.post(
        "/orgs/northstar/roles",
        json={"name": "Chaos", "slug": "chaos", "permissions": ["everything.always"]},
        headers=REVIEWER,
    )
    assert response.status_code == 422


def test_members_pagination_chains(client: TestClient) -> None:
    assert create_org(client).status_code == 201
    for i in range(4):
        client.post(
            "/orgs/northstar/invitations",
            json={"email": f"person{i}@northstar.example"},
            headers=REVIEWER,
        )
    first = client.get("/orgs/northstar/members?limit=2", headers=REVIEWER).json()
    assert len(first["items"]) == 2 and first["has_more"]
    second = client.get(
        f"/orgs/northstar/members?limit=2&cursor={first['next_cursor']}", headers=REVIEWER
    ).json()
    assert len(second["items"]) == 2
    ids = {m["membership_id"] for m in first["items"] + second["items"]}
    assert len(ids) == 4, "pages must not overlap"
