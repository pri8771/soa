"""Provider catalog, managed credentials, and policy lifecycle tests."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.domain.policies import PolicyType, create_policy_draft, publish_policy_draft
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.provider_metrics import record_provider_attempt
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
                "credential_ref": "secretref://memory/orgs/test/providers/main/v1",
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
    assert "persisted worker attempts" in body["health_note"]
    # Nobody is approved and no credential metadata exists yet.
    assert all(item["approved"] is False for item in body["items"])
    assert all(item["credential_configured"] is False for item in body["items"])
    assert all(item["credential_id"] is None for item in body["items"])


async def test_persisted_worker_attempts_drive_health_and_safe_aggregates(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    async with db.session_scope() as session:
        await record_provider_attempt(
            session,
            OrganizationContext(organization_id=org_id),
            capability="field_extraction",
            provider="mock",
            succeeded=True,
            fallback=False,
            latency_ms=12,
            cost_cents=0,
            mean_quality=0.95,
        )
    body = client.get("/orgs/northstar/providers", headers=ADMIN).json()
    mock = next(item for item in body["items"] if item["name"] == "mock")
    assert mock["health"] == "ok"
    assert mock["metrics"] == {
        "attempts": 1,
        "successes": 1,
        "failures": 0,
        "fallbacks": 0,
        "consecutive_failures": 0,
        "mean_latency_ms": 12.0,
        "mean_cost_cents": 0.0,
        "mean_confidence": 0.95,
        "last_attempt_at": mock["metrics"]["last_attempt_at"],
    }


async def test_legacy_approved_provider_reports_configuration_without_exposing_reference(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    await approve_provider(client, db, "tesseract")
    response = client.get("/orgs/northstar/providers", headers=ADMIN)
    by_name = {item["name"]: item for item in response.json()["items"]}
    assert by_name["tesseract"]["approved"] is True
    assert by_name["tesseract"]["credential_configured"] is True
    assert by_name["tesseract"]["credential_id"] is None
    assert "secretref://" not in response.text


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


async def test_managed_credential_and_provider_policy_lifecycle_never_echoes_secret(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    secret = "sk-ant-never-return-this-value"
    stored = client.put(
        "/orgs/northstar/providers/anthropic-claude/credential",
        headers=ADMIN,
        json={"label": "Production Claude", "kind": "api_key", "secret": secret},
    )
    assert stored.status_code == 200, stored.text
    credential_id = stored.json()["credential"]["id"]
    assert stored.json()["credential"]["status"] == "current"
    assert secret not in stored.text
    assert "secretref://" not in stored.text

    credentials = client.get("/orgs/northstar/provider-credentials", headers=ADMIN)
    assert credentials.status_code == 200
    assert credentials.json()["items"][0]["id"] == credential_id
    assert secret not in credentials.text
    assert "secretref://" not in credentials.text

    draft = client.post(
        "/orgs/northstar/policies/provider/drafts",
        headers=ADMIN,
        json={
            "change_summary": "Use Claude with explicit US processing approval",
            "definition": {
                "provider_name": "anthropic-claude",
                "credential_id": credential_id,
                "capabilities": ["ocr", "field_extraction"],
                "allow_third_party_processing": True,
                "allowed_regions": ["us"],
                "estimated_cost_cents": 3,
            },
        },
    )
    assert draft.status_code == 201, draft.text
    policy_id = draft.json()["id"]
    assert draft.json()["state"] == "draft"
    assert draft.json()["definition"]["credential_id"] == credential_id
    assert "credential_ref" not in draft.json()["definition"]
    assert "secretref://" not in draft.text

    validation = client.post(
        f"/orgs/northstar/policies/provider/{policy_id}/validate", headers=ADMIN
    )
    assert validation.status_code == 200, validation.text
    assert validation.json() == {"valid": True, "findings": []}

    published = client.post(f"/orgs/northstar/policies/provider/{policy_id}/publish", headers=ADMIN)
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"
    assert "secretref://" not in published.text

    versions = client.get("/orgs/northstar/policies/provider", headers=ADMIN)
    assert versions.status_code == 200
    assert versions.json()["items"][0]["definition"]["credential_id"] == credential_id
    assert "secretref://" not in versions.text

    catalog = client.get("/orgs/northstar/providers", headers=ADMIN).json()
    claude = next(item for item in catalog["items"] if item["name"] == "anthropic-claude")
    assert claude["approved"] is True
    assert claude["credential_configured"] is True
    assert claude["credential_id"] == credential_id


async def test_invalid_credential_value_is_redacted_from_validation_response(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    canary = "tinykey"
    response = client.put(
        "/orgs/northstar/providers/anthropic-claude/credential",
        headers=ADMIN,
        json={"label": "Invalid", "secret": canary},
    )
    assert response.status_code == 422
    assert canary not in response.text


async def test_rotation_retains_pinned_value_and_guarded_revocation_requires_break_glass(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    first = client.put(
        "/orgs/northstar/providers/google-gemini/credential",
        headers=ADMIN,
        json={"label": "Gemini v1", "secret": "gemini-secret-one"},
    ).json()["credential"]
    draft = client.post(
        "/orgs/northstar/policies/provider/drafts",
        headers=ADMIN,
        json={
            "definition": {
                "provider_name": "google-gemini",
                "credential_id": first["id"],
                "capabilities": ["ocr", "field_extraction"],
                "allow_third_party_processing": True,
                "allowed_regions": ["us"],
            }
        },
    )
    assert draft.status_code == 201, draft.text
    policy_id = draft.json()["id"]
    assert (
        client.post(
            f"/orgs/northstar/policies/provider/{policy_id}/publish", headers=ADMIN
        ).status_code
        == 200
    )
    stale_draft = client.post(
        "/orgs/northstar/policies/provider/drafts",
        headers=ADMIN,
        json={
            "definition": {
                "provider_name": "google-gemini",
                "credential_id": first["id"],
                "capabilities": ["ocr", "field_extraction"],
                "allow_third_party_processing": True,
                "allowed_regions": ["us"],
            }
        },
    )
    assert stale_draft.status_code == 201, stale_draft.text
    stale_policy_id = stale_draft.json()["id"]

    rotated = client.put(
        "/orgs/northstar/providers/google-gemini/credential",
        headers=ADMIN,
        json={"label": "Gemini v2", "secret": "gemini-secret-two"},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["rotated"] is True
    assert rotated.json()["retained_credential_id"] == first["id"]

    stale_validation = client.post(
        f"/orgs/northstar/policies/provider/{stale_policy_id}/validate", headers=ADMIN
    )
    assert stale_validation.status_code == 200
    assert stale_validation.json()["valid"] is False
    stale_publish = client.post(
        f"/orgs/northstar/policies/provider/{stale_policy_id}/publish", headers=ADMIN
    )
    assert stale_publish.status_code == 422
    assert "current and live" in stale_publish.text

    guarded = client.post(
        f"/orgs/northstar/provider-credentials/{first['id']}/revoke",
        headers=ADMIN,
        json={"reason": "rotated"},
    )
    assert guarded.status_code == 409
    assert "referenced by policy versions" in guarded.text

    unconfirmed = client.post(
        f"/orgs/northstar/provider-credentials/{first['id']}/revoke",
        headers=ADMIN,
        json={"reason": "confirmed compromise", "force": True},
    )
    assert unconfirmed.status_code == 422
    assert "confirmation" in unconfirmed.text

    forced = client.post(
        f"/orgs/northstar/provider-credentials/{first['id']}/revoke",
        headers=ADMIN,
        json={"reason": "confirmed compromise", "force": True, "confirmation": "REVOKE"},
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["credential"]["status"] == "revoked"
    assert set(forced.json()["affected_policy_ids"]) == {policy_id, stale_policy_id}
    assert forced.json()["revocation_queued"] is True
    assert "secretref://" not in forced.text


async def test_server_rejects_client_references_cross_provider_ids_and_implicit_hosted_consent(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    credential_id = client.put(
        "/orgs/northstar/providers/anthropic-claude/credential",
        headers=ADMIN,
        json={"label": "Claude", "secret": "claude-secret-value"},
    ).json()["credential"]["id"]
    base = {
        "provider_name": "anthropic-claude",
        "credential_id": credential_id,
        "capabilities": ["ocr", "field_extraction"],
        "allow_third_party_processing": True,
        "allowed_regions": ["us"],
    }
    raw_reference = client.post(
        "/orgs/northstar/policies/provider/drafts",
        headers=ADMIN,
        json={"definition": {**base, "credential_ref": "secretref://memory/forged"}},
    )
    assert raw_reference.status_code == 422
    assert "server-bound" in raw_reference.text

    wrong_provider = client.post(
        "/orgs/northstar/policies/provider/drafts",
        headers=ADMIN,
        json={"definition": {**base, "provider_name": "google-gemini"}},
    )
    assert wrong_provider.status_code == 422
    assert "current live credential" in wrong_provider.text

    no_consent = client.post(
        "/orgs/northstar/policies/provider/drafts",
        headers=ADMIN,
        json={
            "definition": {
                key: value for key, value in base.items() if key != "allow_third_party_processing"
            }
        },
    )
    assert no_consent.status_code == 422
    assert "explicit allow_third_party_processing" in no_consent.text


async def test_confidence_policy_lifecycle_validates_runtime_fields(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    draft = client.post(
        "/orgs/northstar/policies/confidence/drafts",
        headers=ADMIN,
        json={
            "change_summary": "Calibrated review gates",
            "definition": {
                "floor": 0.84,
                "critical_floor": 0.98,
                "field_overrides": {"totals.grand_total": 0.99},
                "critical_requires_evidence": True,
                "critical_candidate_margin": 0.2,
                "standard_candidate_margin": 0.05,
                "review_on_indeterminate_error_rules": True,
            },
        },
    )
    assert draft.status_code == 201, draft.text
    policy_id = draft.json()["id"]
    assert (
        client.post(
            f"/orgs/northstar/policies/confidence/{policy_id}/validate", headers=ADMIN
        ).json()["valid"]
        is True
    )
    published = client.post(
        f"/orgs/northstar/policies/confidence/{policy_id}/publish", headers=ADMIN
    )
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"
