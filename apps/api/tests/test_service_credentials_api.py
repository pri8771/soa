"""Managed service-credential API: one-time secret delivery and tenant fencing."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.auth.errors import AuthenticationError
from soa_api.domain.credentials import ServiceCredential, authenticate_api_key
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent

ADMIN = {"X-Dev-User": "user:admin"}
OTHER_ADMIN = {"X-Dev-User": "user:supervisor"}
REVIEWER = {"X-Dev-User": "user:reviewer"}


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions, dict[str, str]]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/service-credentials.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    client = TestClient(
        create_app(ApiSettings(environment=Environment.TEST), db=db),
        raise_server_exceptions=False,
    )

    stream_ids: dict[str, str] = {}
    for slug, headers in (("northstar", ADMIN), ("other-org", OTHER_ADMIN)):
        assert (
            client.post(
                "/organizations",
                json={"name": slug.title(), "slug": slug},
                headers=headers,
            ).status_code
            == 201
        )
        assert (
            client.post(
                f"/orgs/{slug}/processes",
                json={"name": "Orders", "slug": "orders"},
                headers=headers,
            ).status_code
            == 201
        )
        assert (
            client.post(
                f"/orgs/{slug}/processes/orders/streams",
                json={"name": "API intake", "slug": "api"},
                headers=headers,
            ).status_code
            == 201
        )
        stream_ids[slug] = client.get(f"/orgs/{slug}/streams", headers=headers).json()[0]["id"]

    client.post(
        "/orgs/northstar/invitations",
        json={"email": "reviewer@northstar.example"},
        headers=ADMIN,
    )
    accepted = client.post(
        "/invitations/accept",
        json={"organization_slug": "northstar"},
        headers=REVIEWER,
    )
    assert accepted.status_code == 200, accepted.text
    assert (
        client.post(
            f"/orgs/northstar/members/{accepted.json()['membership_id']}/roles",
            json={"role_slug": "reviewer"},
            headers=ADMIN,
        ).status_code
        == 201
    )
    return client, db, stream_ids


def _create(client: TestClient, stream_id: str):
    return client.post(
        "/orgs/northstar/service-credentials",
        headers=ADMIN,
        json={
            "name": "Warehouse connector",
            "scopes": ["documents.upload"],
            "allowed_stream_ids": [stream_id],
            "expires_in_days": 30,
        },
    )


async def test_create_list_rotate_revoke_never_persist_or_replay_raw_secret(
    harness: tuple[TestClient, DatabaseSessions, dict[str, str]],
) -> None:
    client, db, stream_ids = harness
    created = _create(client, stream_ids["northstar"])
    assert created.status_code == 201, created.text
    assert created.headers["Cache-Control"] == "no-store"
    assert created.headers["Pragma"] == "no-cache"
    first_key = created.json()["api_key"]
    metadata = created.json()["credential"]
    assert first_key.startswith("soa_")
    assert metadata["allowed_stream_ids"] == [stream_ids["northstar"]]
    assert metadata["scopes"] == ["documents.upload"]
    assert metadata["version"] == 1
    assert metadata["expires_at"] is not None
    assert "copy" in created.json()["warning"].lower()

    listing = client.get("/orgs/northstar/service-credentials", headers=ADMIN)
    assert listing.status_code == 200
    assert listing.json()["items"] == [metadata]
    assert first_key not in listing.text
    assert "key_hash" not in listing.text
    assert "api_key" not in listing.text

    credential_id = metadata["id"]
    assert (
        client.post(
            f"/orgs/northstar/service-credentials/{credential_id}/rotate",
            headers=ADMIN,
            json={},
        ).status_code
        == 428
    )
    stale = client.post(
        f"/orgs/northstar/service-credentials/{credential_id}/rotate",
        headers={**ADMIN, "If-Match": "99"},
        json={},
    )
    assert stale.status_code == 409
    rotated = client.post(
        f"/orgs/northstar/service-credentials/{credential_id}/rotate",
        headers={**ADMIN, "If-Match": "1"},
        json={"expires_in_days": 60},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.headers["Cache-Control"] == "no-store"
    second_key = rotated.json()["api_key"]
    assert second_key != first_key
    assert rotated.json()["credential"]["version"] == 2
    assert first_key not in rotated.text

    async with db.session_scope() as session:
        with pytest.raises(AuthenticationError):
            await authenticate_api_key(session, first_key)
        principal = await authenticate_api_key(session, second_key)
        assert principal.subject == credential_id
        row = (await session.execute(select(ServiceCredential))).scalar_one()
        assert first_key not in row.key_hash
        assert second_key not in row.key_hash
        audit_dump = "\n".join(
            str(event.summary)
            for event in (await session.execute(select(AuditEvent))).scalars().all()
        )
        assert first_key not in audit_dump
        assert second_key not in audit_dump

    stale_revoke = client.post(
        f"/orgs/northstar/service-credentials/{credential_id}/revoke",
        headers={**ADMIN, "If-Match": "1"},
    )
    assert stale_revoke.status_code == 409
    revoked = client.post(
        f"/orgs/northstar/service-credentials/{credential_id}/revoke",
        headers={**ADMIN, "If-Match": "2"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["credential"]["status"] == "revoked"
    assert revoked.json()["credential"]["version"] == 3
    assert second_key not in revoked.text
    async with db.session_scope() as session:
        with pytest.raises(AuthenticationError):
            await authenticate_api_key(session, second_key)


def test_permissions_cross_tenant_ids_and_resource_paths_fail_closed(
    harness: tuple[TestClient, DatabaseSessions, dict[str, str]],
) -> None:
    client, _db, stream_ids = harness
    assert (
        client.post(
            "/orgs/northstar/service-credentials",
            headers=REVIEWER,
            json={
                "name": "No grant",
                "allowed_stream_ids": [stream_ids["northstar"]],
            },
        ).status_code
        == 403
    )

    foreign_stream = _create(client, stream_ids["other-org"])
    assert foreign_stream.status_code == 422
    assert client.get("/orgs/northstar/service-credentials", headers=ADMIN).json()["items"] == []

    created = _create(client, stream_ids["northstar"]).json()
    credential_id = created["credential"]["id"]
    assert (
        client.get("/orgs/other-org/service-credentials", headers=OTHER_ADMIN).json()["items"] == []
    )
    cross_tenant_rotate = client.post(
        f"/orgs/other-org/service-credentials/{credential_id}/rotate",
        headers={**OTHER_ADMIN, "If-Match": "1"},
        json={},
    )
    assert cross_tenant_rotate.status_code == 404
    assert created["api_key"] not in cross_tenant_rotate.text


def test_request_contract_rejects_human_permissions_duplicates_and_unknown_fields(
    harness: tuple[TestClient, DatabaseSessions, dict[str, str]],
) -> None:
    client, _db, stream_ids = harness
    stream_id = stream_ids["northstar"]
    overprivileged = client.post(
        "/orgs/northstar/service-credentials",
        headers=ADMIN,
        json={
            "name": "Too broad",
            "scopes": ["credentials.manage"],
            "allowed_stream_ids": [stream_id],
        },
    )
    assert overprivileged.status_code == 422
    duplicate = client.post(
        "/orgs/northstar/service-credentials",
        headers=ADMIN,
        json={
            "name": "Duplicate",
            "allowed_stream_ids": [stream_id, stream_id],
        },
    )
    assert duplicate.status_code == 422
    unknown = client.post(
        "/orgs/northstar/service-credentials",
        headers=ADMIN,
        json={
            "name": "Unknown field",
            "allowed_stream_ids": [stream_id],
            "raw_secret": "must-not-be-accepted-or-echoed",
        },
    )
    assert unknown.status_code == 422
    assert "must-not-be-accepted-or-echoed" not in unknown.text
