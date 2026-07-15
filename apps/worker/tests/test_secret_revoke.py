"""Rotated credentials are revoked only by a durable post-commit job."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_config import MemorySecretStore, SecretNotFoundError, SecretReference
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.integrations import (
    IntegrationCredentialRepository,
    create_integration,
    store_integration_credential,
)
from soa_db.jobs import Job
from soa_db.provider_credentials import (
    ProviderCredentialRepository,
    revoke_provider_credential,
    store_provider_credential,
)
from soa_db.repository import OrganizationContext
from soa_worker.registry import HandlerRegistry, JobEnvelope
from soa_worker.secret_revoke import register_secret_revoke_handler

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/secret-revoke.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_rotation_job_validates_then_revokes_idempotently(db: DatabaseSessions) -> None:
    secrets = MemorySecretStore()
    async with db.session_scope() as session:
        integration = await create_integration(
            session,
            CONTEXT,
            name="ERP",
            slug="erp",
            integration_type="webhook",
            actor_id="user:test",
        )
        first = await store_integration_credential(
            session,
            CONTEXT,
            integration=integration,
            kind="webhook_hmac_secret",
            secret="original-secret",
            actor_id="user:test",
            secret_store=secrets,
        )
        first_reference = first.secret_reference
        await store_integration_credential(
            session,
            CONTEXT,
            integration=integration,
            kind="webhook_hmac_secret",
            secret="rotated-secret",
            actor_id="user:test",
            secret_store=secrets,
        )

    # Commit has completed, but no synchronous external deletion occurred.
    assert await secrets.resolve(SecretReference.parse(first_reference)) == "original-secret"
    async with db.session_scope() as session:
        job = (
            await session.execute(select(Job).where(Job.job_type == "secret.revoke"))
        ).scalar_one()
        credential = await IntegrationCredentialRepository(session, CONTEXT).get(first.id)
        assert credential is not None and credential.revoked_at is not None
        envelope = JobEnvelope(
            job_id=job.id,
            job_type=job.job_type,
            payload=dict(job.payload),
            organization_id=job.organization_id,
        )

    registry = HandlerRegistry()
    register_secret_revoke_handler(registry, db, secrets)
    handler = registry.resolve("secret.revoke")
    await handler(envelope)
    await handler(envelope)  # queue redelivery is harmless
    with pytest.raises(SecretNotFoundError):
        await secrets.resolve(SecretReference.parse(first_reference))


async def test_rotation_job_refuses_cross_tenant_metadata(db: DatabaseSessions) -> None:
    secrets = MemorySecretStore()
    registry = HandlerRegistry()
    register_secret_revoke_handler(registry, db, secrets)
    with pytest.raises(ValueError, match="does not belong"):
        await registry.resolve("secret.revoke")(
            JobEnvelope(
                job_type="secret.revoke",
                organization_id=uuid.uuid4(),
                payload={
                    "credential_id": str(uuid.uuid4()),
                    "secret_reference": "secretref://memory/orgs/fake/secret",
                },
            )
        )


async def test_provider_revocation_job_validates_then_revokes_idempotently(
    db: DatabaseSessions,
) -> None:
    secrets = MemorySecretStore()
    async with db.session_scope() as session:
        credential, _ = await store_provider_credential(
            session,
            CONTEXT,
            provider_name="anthropic-claude",
            label="Production Claude",
            kind="api_key",
            secret="provider-secret",
            actor_id="user:test",
            secret_store=secrets,
        )
        reference = credential.secret_reference
        await revoke_provider_credential(
            session,
            CONTEXT,
            credential=credential,
            reason="credential was replaced",
            actor_id="user:test",
            referenced_policy_ids=[],
        )

    assert await secrets.resolve(SecretReference.parse(reference)) == "provider-secret"
    async with db.session_scope() as session:
        jobs = list(
            (await session.execute(select(Job).where(Job.job_type == "secret.revoke")))
            .scalars()
            .all()
        )
        job = next(item for item in jobs if item.payload.get("credential_type") == "provider")
        stored = await ProviderCredentialRepository(session, CONTEXT).get(credential.id)
        assert stored is not None and stored.revoked_at is not None
        envelope = JobEnvelope(
            job_id=job.id,
            job_type=job.job_type,
            payload=dict(job.payload),
            organization_id=job.organization_id,
        )

    registry = HandlerRegistry()
    register_secret_revoke_handler(registry, db, secrets)
    handler = registry.resolve("secret.revoke")
    await handler(envelope)
    await handler(envelope)
    with pytest.raises(SecretNotFoundError):
        await secrets.resolve(SecretReference.parse(reference))


async def test_revocation_job_rejects_unknown_credential_type(db: DatabaseSessions) -> None:
    registry = HandlerRegistry()
    register_secret_revoke_handler(registry, db, MemorySecretStore())
    with pytest.raises(ValueError, match="credential_type"):
        await registry.resolve("secret.revoke")(
            JobEnvelope(
                job_type="secret.revoke",
                organization_id=ORG,
                payload={
                    "credential_type": "unbounded-model-name",
                    "credential_id": str(uuid.uuid4()),
                    "secret_reference": "secretref://memory/orgs/fake/secret",
                },
            )
        )
