"""Mapping profile API tests (EXP-003): draft/validate/publish/compare,
secret hygiene in every response, and optimistic concurrency."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.routers.integrations import DEFAULT_SAMPLE
from soa_api.settings import ApiSettings, Environment
from soa_canonical import validate_order
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:admin"}
AUDITOR = {"X-Dev-User": "user:auditor"}  # integrations.read, no manage
REVIEWER = {"X-Dev-User": "user:reviewer"}  # no integrations permissions at all

GOOD_DEFINITION = {
    "fields": [
        {"target": "PoNumber", "source": "identifiers.po_number", "required": True},
        {
            "target": "OrderDate",
            "source": "dates.order_date",
            "format": {"kind": "date", "pattern": "MM/DD/YYYY"},
        },
        {"target": "Total", "source": "totals.grand_total.amount"},
    ],
    "constants": [{"target": "SourceSystem", "value": "SOA"}],
}
TARGET_SCHEMA = {"type": "object", "required": ["PoNumber", "SourceSystem"]}


@pytest.fixture
async def client(tmp_path: Path) -> TestClient:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/integrations-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(
        ApiSettings(environment=Environment.TEST),
        db=DatabaseSessions(engine),
        object_store=MemoryObjectStore(),
    )
    test_client = TestClient(app, raise_server_exceptions=False)
    assert (
        test_client.post(
            "/organizations", json={"name": "Northstar", "slug": "northstar"}, headers=ADMIN
        ).status_code
        == 201
    )
    for headers, email, role in (
        (AUDITOR, "auditor@northstar.example", "auditor"),
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


def make_integration(client: TestClient) -> None:
    created = client.post(
        "/orgs/northstar/integrations",
        json={
            "name": "Northstar ERP webhook",
            "slug": "erp",
            "integration_type": "webhook",
            "endpoint_url": "https://erp.northstar.example/orders",
        },
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text


def test_default_sample_is_a_valid_canonical_order() -> None:
    validate_order(DEFAULT_SAMPLE)


def test_integration_lifecycle_and_secret_hygiene(client: TestClient) -> None:
    make_integration(client)
    # Unknown types fail closed; duplicate slugs conflict.
    unknown = client.post(
        "/orgs/northstar/integrations",
        json={"name": "X", "slug": "x", "integration_type": "sap-rfc"},
        headers=ADMIN,
    )
    assert unknown.status_code == 422
    duplicate = client.post(
        "/orgs/northstar/integrations",
        json={"name": "Y", "slug": "erp", "integration_type": "webhook"},
        headers=ADMIN,
    )
    assert duplicate.status_code == 409

    # Credential set + rotation: acknowledged, NEVER echoed.
    first = client.put(
        "/orgs/northstar/integrations/erp/credential",
        json={"kind": "webhook_hmac_secret", "secret": "whsec_super_secret_value"},
        headers=ADMIN,
    )
    assert first.status_code == 200, first.text
    assert first.json() == {
        "credential_configured": True,
        "kind": "webhook_hmac_secret",
        "rotated": False,
    }
    assert "whsec_super_secret_value" not in first.text
    second = client.put(
        "/orgs/northstar/integrations/erp/credential",
        json={"kind": "webhook_hmac_secret", "secret": "whsec_rotated_secret_value"},
        headers=ADMIN,
    )
    assert second.json()["rotated"] is True

    # No response anywhere carries the secret.
    detail = client.get("/orgs/northstar/integrations/erp", headers=ADMIN)
    assert detail.status_code == 200
    assert detail.json()["integration"]["credential_configured"] is True
    assert "whsec" not in detail.text
    listing = client.get("/orgs/northstar/integrations", headers=ADMIN)
    assert "whsec" not in listing.text

    # Permissions: read-only role reads, cannot manage; no-permission
    # role sees nothing.
    assert client.get("/orgs/northstar/integrations", headers=AUDITOR).status_code == 200
    assert (
        client.post(
            "/orgs/northstar/integrations",
            json={"name": "Z", "slug": "z", "integration_type": "webhook"},
            headers=AUDITOR,
        ).status_code
        == 403
    )
    assert client.get("/orgs/northstar/integrations", headers=REVIEWER).status_code == 403


def test_mapping_draft_validate_publish_compare(client: TestClient) -> None:
    make_integration(client)
    draft = client.post(
        "/orgs/northstar/integrations/erp/mapping-versions",
        json={"definition": GOOD_DEFINITION, "target_schema": TARGET_SCHEMA},
        headers=ADMIN,
    ).json()
    version_id = draft["id"]
    base = f"/orgs/northstar/integrations/erp/mapping-versions/{version_id}"

    # Optimistic concurrency: a stale If-Match is refused.
    stale = client.patch(
        base,
        json={"definition": GOOD_DEFINITION, "target_schema": TARGET_SCHEMA},
        headers={**ADMIN, "If-Match": "99"},
    )
    assert stale.status_code == 409
    fresh = client.patch(
        base,
        json={"definition": GOOD_DEFINITION, "target_schema": TARGET_SCHEMA},
        headers={**ADMIN, "If-Match": str(draft["version"])},
    )
    assert fresh.status_code == 200, fresh.text

    # Validation executes the REAL engine over the built-in sample.
    validation = client.post(f"{base}/validate", json={}, headers=ADMIN).json()
    assert validation["valid"] is True
    assert validation["payload"]["PoNumber"] == "PO-100042"
    assert validation["payload"]["OrderDate"] == "03/14/2026"
    assert validation["payload"]["SourceSystem"] == "SOA"
    assert any(entry["target"] == "OrderDate" for entry in validation["trace"])

    # A custom sample is validated as a canonical order first.
    bad_sample = client.post(f"{base}/validate", json={"sample": {"nope": True}}, headers=ADMIN)
    assert bad_sample.status_code == 422

    # A broken definition validates false with named errors — and
    # cannot publish.
    broken = {"fields": [{"target": "X", "source": "a.b", "format": {"kind": "python_eval"}}]}
    assert (
        client.patch(
            base, json={"definition": broken, "target_schema": {}}, headers=ADMIN
        ).status_code
        == 200
    )
    invalid = client.post(f"{base}/validate", json={}, headers=ADMIN).json()
    assert invalid["valid"] is False
    assert any("python_eval" in error for error in invalid["errors"])
    refused = client.post(f"{base}/publish", headers=ADMIN)
    assert refused.status_code == 422

    # Restore, publish, and prove immutability + concurrency together.
    assert (
        client.patch(
            base,
            json={"definition": GOOD_DEFINITION, "target_schema": TARGET_SCHEMA},
            headers=ADMIN,
        ).status_code
        == 200
    )
    published = client.post(f"{base}/publish", headers=ADMIN)
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"
    immutable = client.patch(
        base,
        json={"definition": GOOD_DEFINITION, "target_schema": TARGET_SCHEMA},
        headers=ADMIN,
    )
    assert immutable.status_code == 409
    assert "immutable" in immutable.json()["error"]["message"]

    # Second version changes one field and adds one; compare names both.
    changed_definition = {
        "fields": [
            {"target": "PoNumber", "source": "identifiers.po_number", "required": True},
            {
                "target": "OrderDate",
                "source": "dates.order_date",
                "format": {"kind": "date", "pattern": "DD.MM.YYYY"},
            },
            {"target": "Total", "source": "totals.grand_total.amount"},
            {"target": "Currency", "source": "terms.currency"},
        ],
        "constants": [{"target": "SourceSystem", "value": "SOA"}],
    }
    second = client.post(
        "/orgs/northstar/integrations/erp/mapping-versions",
        json={"definition": changed_definition, "target_schema": TARGET_SCHEMA},
        headers=ADMIN,
    ).json()
    assert second["version_number"] == 2
    compare = client.get(
        "/orgs/northstar/integrations/erp/mapping-versions/compare"
        f"?from={version_id}&to={second['id']}",
        headers=ADMIN,
    ).json()
    assert compare["added"] == ["Currency"]
    assert compare["removed"] == []
    assert [entry["target"] for entry in compare["changed"]] == ["OrderDate"]
    assert compare["target_schema_changed"] is False

    # Publishing v2 supersedes v1 and moves the active pointer.
    assert (
        client.post(
            f"/orgs/northstar/integrations/erp/mapping-versions/{second['id']}/publish",
            headers=ADMIN,
        ).status_code
        == 200
    )
    detail = client.get("/orgs/northstar/integrations/erp", headers=ADMIN).json()
    states = {v["version_number"]: v["state"] for v in detail["mapping_versions"]}
    assert states == {1: "superseded", 2: "published"}
    assert detail["integration"]["active_mapping_version_id"] == second["id"]
