"""Provider credential metadata keeps values out of tenant storage."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_config import MemorySecretStore, SecretNotFoundError, SecretReference
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.jobs import Job
from soa_db.provider_credentials import (
    ProviderCredential,
    ProviderCredentialConflictError,
    ProviderCredentialRepository,
    revoke_provider_credential,
    store_provider_credential,
)
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import RLS_PROTECTED_TABLES

ORG = OrganizationContext(organization_id=uuid.UUID("11111111-1111-4111-8111-111111111111"))
OTHER = OrganizationContext(organization_id=uuid.UUID("22222222-2222-4222-8222-222222222222"))


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/provider-credentials.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_rotation_retains_old_pin_but_database_never_stores_values(
    db: DatabaseSessions,
) -> None:
    secrets = MemorySecretStore()
    async with db.session_scope() as session:
        first, previous = await store_provider_credential(
            session,
            ORG,
            provider_name="anthropic-claude",
            label="Claude v1",
            kind="api_key",
            secret="first-provider-value",
            actor_id="user:admin",
            secret_store=secrets,
        )
        assert previous is None
        second, previous = await store_provider_credential(
            session,
            ORG,
            provider_name="anthropic-claude",
            label="Claude v2",
            kind="api_key",
            secret="second-provider-value",
            actor_id="user:admin",
            secret_store=secrets,
        )
        assert previous == first.id
        first_reference = first.secret_reference
        second_reference = second.secret_reference

    assert await secrets.resolve(SecretReference.parse(first_reference)) == "first-provider-value"
    assert await secrets.resolve(SecretReference.parse(second_reference)) == "second-provider-value"
    async with db.session_scope() as session:
        rows = list((await session.execute(select(ProviderCredential))).scalars().all())
        assert len(rows) == 2
        assert all("provider-value" not in row.secret_reference for row in rows)
        stored_first = await ProviderCredentialRepository(session, ORG).get(first.id)
        assert stored_first is not None and stored_first.status == "superseded"
        assert await ProviderCredentialRepository(session, OTHER).get(first.id) is None


async def test_failed_database_unit_of_work_revokes_new_external_secret(
    db: DatabaseSessions,
) -> None:
    secrets = MemorySecretStore()
    reference = ""
    with pytest.raises(RuntimeError, match="rollback provider credential"):
        async with db.session_scope() as session:
            credential, _ = await store_provider_credential(
                session,
                ORG,
                provider_name="anthropic-claude",
                label="Rolled back",
                kind="api_key",
                secret="must-not-be-orphaned",
                actor_id="user:admin",
                secret_store=secrets,
            )
            reference = credential.secret_reference
            raise RuntimeError("rollback provider credential")

    with pytest.raises(SecretNotFoundError):
        await secrets.resolve(SecretReference.parse(reference))
    async with db.session_scope() as session:
        rows = list((await session.execute(select(ProviderCredential))).scalars().all())
        assert rows == []


async def test_guarded_revoke_requires_force_for_policy_references_and_queues_delete(
    db: DatabaseSessions,
) -> None:
    secrets = MemorySecretStore()
    policy_id = uuid.uuid4()
    async with db.session_scope() as session:
        credential, _ = await store_provider_credential(
            session,
            ORG,
            provider_name="google-gemini",
            label="Gemini",
            kind="api_key",
            secret="gemini-provider-value",
            actor_id="user:admin",
            secret_store=secrets,
        )
        with pytest.raises(ProviderCredentialConflictError, match="referenced by policy"):
            await revoke_provider_credential(
                session,
                ORG,
                credential=credential,
                reason="rotated",
                actor_id="user:admin",
                referenced_policy_ids=[policy_id],
            )
        revoked = await revoke_provider_credential(
            session,
            ORG,
            credential=credential,
            reason="confirmed compromise",
            actor_id="user:admin",
            referenced_policy_ids=[policy_id],
            force=True,
        )
        reference = revoked.secret_reference

    # External deletion is post-commit and worker-driven.
    assert await secrets.resolve(SecretReference.parse(reference)) == "gemini-provider-value"
    async with db.session_scope() as session:
        job = (
            await session.execute(
                select(Job).where(Job.dedupe_key == f"secret.revoke:provider:{credential.id}")
            )
        ).scalar_one()
        assert job.payload["credential_type"] == "provider"
        assert job.payload["secret_reference"] == reference

    await secrets.revoke(SecretReference.parse(reference))
    with pytest.raises(SecretNotFoundError):
        await secrets.resolve(SecretReference.parse(reference))


def test_provider_credentials_are_registered_for_rls() -> None:
    assert "provider_credentials" in RLS_PROTECTED_TABLES
