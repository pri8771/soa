"""Policy root tests (CFG-005)."""

import uuid
from pathlib import Path

import pytest

from soa_api.domain.policies import (
    PolicyType,
    PolicyValidationError,
    PolicyVersionRepository,
    create_policy_draft,
    policy_reference,
    publish_policy_draft,
    validate_policy,
)
from soa_api.domain.versioning import VersionState
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext

ORG_A = OrganizationContext(organization_id=uuid.UUID(int=0xA))
ACTOR = "user:test-admin"

PROVIDER_OK = {
    "provider_name": "acme-docai",
    "capabilities": ["ocr", "field_extraction", "tables"],
    "credential_ref": "secretref://memory/orgs/acme/providers/docai/v1",
}


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/policies.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


def test_embedded_secrets_rejected_everywhere() -> None:
    with pytest.raises(PolicyValidationError, match="never embedded"):
        validate_policy(
            PolicyType.PROVIDER,
            {"provider_name": "x", "capabilities": [], "api_key": "sk-123"},
        )
    with pytest.raises(PolicyValidationError, match="never embedded"):
        validate_policy(
            PolicyType.MAPPING,
            {"mappings": {"erp": {"auth": {"password": "hunter2"}}}},
        )
    # credential_ref itself is fine — it is a reference, not a secret.
    validate_policy(PolicyType.PROVIDER, PROVIDER_OK)


def test_policy_shape_validation() -> None:
    with pytest.raises(PolicyValidationError, match="between 0 and 1"):
        validate_policy(PolicyType.CONFIDENCE, {"floor": 1.5})
    validate_policy(PolicyType.CONFIDENCE, {"floor": 0.8, "field_overrides": {"total": 0.95}})
    with pytest.raises(PolicyValidationError, match="positive integer"):
        validate_policy(PolicyType.RETENTION, {"document_days": 0})
    with pytest.raises(PolicyValidationError, match="mappings object"):
        validate_policy(PolicyType.MAPPING, {})
    with pytest.raises(PolicyValidationError, match="secretref"):
        validate_policy(
            PolicyType.PROVIDER,
            {"provider_name": "x", "capabilities": [], "credential_ref": "sk-raw-secret"},
        )


async def test_missing_capabilities_block_publish(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        draft = await create_policy_draft(
            session,
            ORG_A,
            policy_type=PolicyType.PROVIDER,
            definition={"provider_name": "weak-ocr", "capabilities": ["ocr"]},
            actor_id=ACTOR,
        )
        with pytest.raises(PolicyValidationError, match="publish blocked"):
            await publish_policy_draft(session, ORG_A, draft=draft, actor_id=ACTOR)


async def test_publish_supersedes_per_policy_type(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        provider = await create_policy_draft(
            session,
            ORG_A,
            policy_type=PolicyType.PROVIDER,
            definition=PROVIDER_OK,
            actor_id=ACTOR,
        )
        await publish_policy_draft(session, ORG_A, draft=provider, actor_id=ACTOR)
        confidence = await create_policy_draft(
            session,
            ORG_A,
            policy_type=PolicyType.CONFIDENCE,
            definition={"floor": 0.8},
            actor_id=ACTOR,
        )
        await publish_policy_draft(session, ORG_A, draft=confidence, actor_id=ACTOR)
        first_provider_id = provider.id
    async with db.session_scope() as session:
        second = await create_policy_draft(
            session,
            ORG_A,
            policy_type=PolicyType.PROVIDER,
            definition=PROVIDER_OK,
            actor_id=ACTOR,
        )
        await publish_policy_draft(session, ORG_A, draft=second, actor_id=ACTOR)
    async with db.session_scope() as session:
        repo = PolicyVersionRepository(session, ORG_A)
        first = await repo.get(first_provider_id)
        assert first is not None and first.state == VersionState.SUPERSEDED
        published_provider = await repo.get_published(PolicyType.PROVIDER)
        assert published_provider is not None and published_provider.version_number == 2
        published_confidence = await repo.get_published(PolicyType.CONFIDENCE)
        assert published_confidence is not None, "other policy types are unaffected"
        reference = policy_reference(published_provider)
        assert reference["policy_type"] == "provider"
        assert reference["policy_version_id"] == str(published_provider.id)
