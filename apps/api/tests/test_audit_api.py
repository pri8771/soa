"""Audit query and export tests (ANA-007): filtered keyset pagination,
authorization, the integrity manifest (verifiable hashes over the
stored NDJSON chunks), large exports chunking into multiple files, and
the over-cap refusal."""

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import ActorType, record_audit_event
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:admin"}


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions, MemoryObjectStore]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/audit-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    store = MemoryObjectStore()
    app = create_app(ApiSettings(environment=Environment.TEST), db=db, object_store=store)
    client = TestClient(app, raise_server_exceptions=False)
    assert (
        client.post(
            "/organizations", json={"name": "Northstar", "slug": "northstar"}, headers=ADMIN
        ).status_code
        == 201
    )
    return client, db, store


async def seed_events(client: TestClient, db: DatabaseSessions, count: int) -> None:
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    async with db.session_scope() as session:
        for index in range(count):
            await record_audit_event(
                session,
                actor_type=ActorType.SYSTEM,
                actor_id="system:test",
                action="document.state_changed" if index % 2 == 0 else "catalog.match_selected",
                target_type="document",
                target_id=f"doc-{index}",
                organization_id=org_id,
                summary={"index": index},
            )


async def test_query_filters_and_paginates(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, _ = harness
    await seed_events(client, db, 10)

    filtered = client.get(
        "/orgs/northstar/audit-events",
        params={"action": "document."},
        headers=ADMIN,
    ).json()
    # Org creation etc. also write audit rows; the filter isolates ours.
    assert all(item["action"].startswith("document.") for item in filtered["items"])
    assert len(filtered["items"]) == 5

    first = client.get(
        "/orgs/northstar/audit-events",
        params={"action": "document.", "limit": 2},
        headers=ADMIN,
    ).json()
    assert first["has_more"] is True
    second = client.get(
        "/orgs/northstar/audit-events",
        params={"action": "document.", "limit": 2, "cursor": first["next_cursor"]},
        headers=ADMIN,
    ).json()
    ids = {item["id"] for item in first["items"]} | {item["id"] for item in second["items"]}
    assert len(ids) == 4  # no overlap across pages

    malformed = client.get(
        "/orgs/northstar/audit-events", params={"cursor": "nonsense"}, headers=ADMIN
    )
    assert malformed.status_code == 400


async def test_query_and_export_are_permissioned(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, _, _ = harness
    # A member whose role lacks audit.read is refused.
    client.post(
        "/orgs/northstar/invitations",
        json={"email": "reviewer@northstar.example"},
        headers=ADMIN,
    )
    reviewer = {"X-Dev-User": "user:reviewer"}
    accepted = client.post(
        "/invitations/accept", json={"organization_slug": "northstar"}, headers=reviewer
    )
    granted = client.post(
        f"/orgs/northstar/members/{accepted.json()['membership_id']}/roles",
        json={"role_slug": "reviewer"},
        headers=ADMIN,
    )
    assert granted.status_code == 201, granted.text
    assert client.get("/orgs/northstar/audit-events", headers=reviewer).status_code == 403
    assert (
        client.post("/orgs/northstar/audit-exports", json={}, headers=reviewer).status_code == 403
    )
    outsider = {"X-Dev-User": "user:integration-admin"}
    assert client.get("/orgs/northstar/audit-events", headers=outsider).status_code == 404


async def test_export_bundle_has_a_verifiable_hash_manifest(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    await seed_events(client, db, 6)
    response = client.post(
        "/orgs/northstar/audit-exports",
        json={"action": "document."},
        headers=ADMIN,
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["event_count"] == 3
    (entry,) = payload["files"]

    # The stored chunk hashes to exactly what the manifest claims.
    org_id = client.get("/orgs/northstar", headers=ADMIN).json()["id"]
    key = f"audit-exports/{org_id}/{payload['export_id']}/{entry['name']}"
    data = await store.get(key)
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert len(data) == entry["bytes"]
    lines = [json.loads(line) for line in data.decode().splitlines()]
    assert len(lines) == 3
    assert all(line["action"].startswith("document.") for line in lines)

    # The manifest itself is stored and hash-stamped.
    manifest = json.loads(
        await store.get(f"audit-exports/{org_id}/{payload['export_id']}/manifest.json")
    )
    assert manifest["event_count"] == 3
    assert manifest["files"][0]["sha256"] == entry["sha256"]
    assert "download_url" not in manifest["files"][0]

    # Signed URLs, not raw keys, are handed out.
    assert "sig" in payload["manifest_download_url"] or "?" in payload["manifest_download_url"]

    # The export itself is on the audit trail (counts only).
    trail = client.get(
        "/orgs/northstar/audit-events", params={"action": "audit.export"}, headers=ADMIN
    ).json()
    assert trail["items"][0]["summary"]["event_count"] == 3
    assert trail["items"][0]["summary"]["manifest_sha256"] == payload["manifest_sha256"]


async def test_large_exports_chunk_and_over_cap_is_refused(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from soa_api.routers import audit as audit_router

    client, db, _ = harness
    await seed_events(client, db, 25)
    monkeypatch.setattr(audit_router, "EXPORT_CHUNK_EVENTS", 10)
    response = client.post("/orgs/northstar/audit-exports", json={}, headers=ADMIN)
    assert response.status_code == 201
    payload = response.json()
    # 25 seeded + the org-creation trail events chunk at 10 per file.
    assert len(payload["files"]) >= 3
    assert sum(entry["events"] for entry in payload["files"]) == payload["event_count"]
    names = [entry["name"] for entry in payload["files"]]
    assert names == sorted(names)

    monkeypatch.setattr(audit_router, "MAX_EXPORT_EVENTS", 5)
    refused = client.post("/orgs/northstar/audit-exports", json={}, headers=ADMIN)
    assert refused.status_code == 413
    assert "narrow" in refused.json()["error"]["message"]
