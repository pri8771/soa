"""Full authorization flow against real PostgreSQL RLS (TEN-007/TEN-010).

SQLite tests cannot catch GUC-binding ordering bugs: the authorization
service reads FORCE-RLS tables (memberships, role_assignments, roles), so
it must bind the user GUC before the membership lookup and the tenant GUC
before the permission reads — or every request 404s as NOT_A_MEMBER in
production while passing every SQLite test. This drives the real HTTP
surface end-to-end as the non-superuser application role.
"""

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import DatabaseSessions

POSTGRES_URL = os.environ.get("SOA_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="SOA_TEST_POSTGRES_URL not set; RLS authz flow requires real PostgreSQL",
    ),
]

ADMIN = {"X-Dev-User": "user:reviewer"}
NON_MEMBER = {"X-Dev-User": "user:supervisor"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    assert POSTGRES_URL is not None
    # NullPool + the TestClient context manager keep every asyncpg
    # connection inside one event loop: pooled connections created in one
    # request's loop must never be reused from another ("Future attached
    # to a different loop").
    engine = create_async_engine(POSTGRES_URL, poolclass=NullPool)
    db = DatabaseSessions(engine)
    app = create_app(ApiSettings(environment=Environment.TEST), db=db)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_full_tenant_flow_under_rls(client: TestClient) -> None:
    slug = f"authz-{uuid.uuid4().hex[:8]}"

    # Creating an organization inserts into RLS-protected tables
    # (memberships, roles, role_assignments) — WITH CHECK must pass.
    created = client.post(
        "/organizations", json={"name": "Authz Probe", "slug": slug}, headers=ADMIN
    )
    assert created.status_code == 201, created.text

    # The creator must be able to authorize into the org: this exercises
    # bind_user before the membership lookup and bind_tenant before the
    # role/permission reads.
    org = client.get(f"/orgs/{slug}", headers=ADMIN)
    assert org.status_code == 200, org.text

    members = client.get(f"/orgs/{slug}/members", headers=ADMIN)
    assert members.status_code == 200, members.text
    assert len(members.json()["items"]) == 1

    # Permission-guarded feature surface works under RLS too.
    stats = client.get(f"/orgs/{slug}/jobs/stats", headers=ADMIN)
    assert stats.status_code == 200, stats.text

    # A different principal with no membership stays locked out (404 so
    # organization existence does not leak).
    assert client.get(f"/orgs/{slug}", headers=NON_MEMBER).status_code == 404

    # /me resolves memberships and permissions under user binding.
    me = client.get("/me", headers=ADMIN)
    assert me.status_code == 200, me.text
    membership = next(m for m in me.json()["memberships"] if m["organization_slug"] == slug)
    assert "organization.read" in membership["permissions"]
