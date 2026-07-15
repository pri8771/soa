"""Integration and mapping-profile model tests (EXP-001): fail-closed
types, reference-based credentials (SEC-005 — values live in the secret
store, rotation, audit hygiene), and the published-mapping immutability
discipline."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_api.domain.integrations import (
    IntegrationCredentialRepository,
    IntegrationRepository,
    MappingProfileVersionRepository,
    UnknownIntegrationTypeError,
    create_integration,
    create_mapping_draft,
    credential_secret_for_delivery,
    publish_mapping_draft,
    store_integration_credential,
)
from soa_api.domain.versioning import ImmutableVersionError, InvalidVersionStateError
from soa_config import MemorySecretStore, SecretNotFoundError, SecretReference
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.jobs import Job
from soa_db.repository import OrganizationContext

ORG_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
CONTEXT = OrganizationContext(organization_id=ORG_A)

MAPPING = {"fields": [{"target": "PoNumber", "source": "identifiers.po_number"}]}
TARGET = {"type": "object", "required": ["PoNumber"]}


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/integrations.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def make_integration(db: DatabaseSessions) -> uuid.UUID:
    async with db.session_scope() as session:
        integration = await create_integration(
            session,
            CONTEXT,
            name="Northstar ERP webhook",
            slug="northstar-erp",
            integration_type="webhook",
            endpoint_url="https://erp.northstar.example/orders",
            actor_id="user:x",
        )
        return integration.id


async def test_unknown_integration_types_fail_closed(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        with pytest.raises(UnknownIntegrationTypeError, match="sap-rfc"):
            await create_integration(
                session,
                CONTEXT,
                name="X",
                slug="x",
                integration_type="sap-rfc",
                actor_id="user:x",
            )


async def test_credentials_are_references_rotated_and_never_audited(
    db: DatabaseSessions,
) -> None:
    integration_id = await make_integration(db)
    secrets = MemorySecretStore()
    async with db.session_scope() as session:
        integration = await IntegrationRepository(session, CONTEXT).get(integration_id)
        assert integration is not None
        first = await store_integration_credential(
            session,
            CONTEXT,
            integration=integration,
            kind="webhook_hmac_secret",
            secret="whsec_original_value",
            actor_id="user:x",
            secret_store=secrets,
        )
        # The database row carries a REFERENCE — the value lives only in
        # the secret store (SEC-005).
        assert integration.credential_id == first.id
        assert first.secret_reference.startswith("secretref://memory/orgs/")
        assert "whsec_original_value" not in repr(vars(first))
        first_reference = first.secret_reference
        assert (
            await credential_secret_for_delivery(
                session, CONTEXT, integration=integration, secret_store=secrets
            )
            == "whsec_original_value"
        )

        rotated = await store_integration_credential(
            session,
            CONTEXT,
            integration=integration,
            kind="webhook_hmac_secret",
            secret="whsec_rotated_value",
            actor_id="user:x",
            secret_store=secrets,
        )
        assert integration.credential_id == rotated.id
        assert rotated.secret_reference != first_reference, "rotation issues a fresh reference"
        stale = await IntegrationCredentialRepository(session, CONTEXT).get(first.id)
        assert stale is not None and stale.revoked_at is not None
        # The old value remains live until the DB transaction commits and
        # the durable post-commit revocation job runs. It is never deleted
        # synchronously while a rollback could restore this DB pointer.
        assert (
            await secrets.resolve(SecretReference.parse(first_reference)) == "whsec_original_value"
        )
        revoke_job = (
            await session.execute(select(Job).where(Job.job_type == "secret.revoke"))
        ).scalar_one()
        assert revoke_job.organization_id == ORG_A
        assert revoke_job.payload["credential_id"] == str(first.id)
        assert revoke_job.payload["secret_reference"] == first_reference
        assert (
            await credential_secret_for_delivery(
                session, CONTEXT, integration=integration, secret_store=secrets
            )
            == "whsec_rotated_value"
        )

        with pytest.raises(ValueError, match="non-empty secret"):
            await store_integration_credential(
                session,
                CONTEXT,
                integration=integration,
                kind="webhook_hmac_secret",
                secret="   ",
                actor_id="user:x",
                secret_store=secrets,
            )

        # A configured credential whose stored value vanished is an
        # inconsistency that raises — never a silent "no credential".
        await secrets.revoke(SecretReference.parse(rotated.secret_reference))
        with pytest.raises(SecretNotFoundError, match="must be re-set"):
            await credential_secret_for_delivery(
                session, CONTEXT, integration=integration, secret_store=secrets
            )

        # Audit records THAT credentials changed — never their values.
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action.like("%credential%"))
                )
            )
            .scalars()
            .all()
        )
        assert {event.action for event in events} == {
            "integration.credential_set",
            "integration.credential_rotated",
        }
        assert "whsec" not in str([event.summary for event in events])


async def test_failed_database_unit_of_work_revokes_new_integration_secret(
    db: DatabaseSessions,
) -> None:
    integration_id = await make_integration(db)
    secrets = MemorySecretStore()
    reference = ""
    with pytest.raises(RuntimeError, match="rollback integration credential"):
        async with db.session_scope() as session:
            integration = await IntegrationRepository(session, CONTEXT).get(integration_id)
            assert integration is not None
            credential = await store_integration_credential(
                session,
                CONTEXT,
                integration=integration,
                kind="webhook_hmac_secret",
                secret="must-not-be-orphaned",
                actor_id="user:x",
                secret_store=secrets,
            )
            reference = credential.secret_reference
            raise RuntimeError("rollback integration credential")

    with pytest.raises(SecretNotFoundError):
        await secrets.resolve(SecretReference.parse(reference))


async def test_published_mappings_are_immutable_and_superseded_in_order(
    db: DatabaseSessions,
) -> None:
    integration_id = await make_integration(db)
    async with db.session_scope() as session:
        repo = IntegrationRepository(session, CONTEXT)
        integration = await repo.get(integration_id)
        assert integration is not None
        draft = await create_mapping_draft(
            session,
            CONTEXT,
            integration=integration,
            definition=MAPPING,
            target_schema=TARGET,
            actor_id="user:x",
        )
        # Drafts edit freely.
        draft.definition = {**MAPPING, "constants": [{"target": "Source", "value": "SOA"}]}
        await session.flush()
        published = await publish_mapping_draft(
            session, CONTEXT, integration=integration, draft=draft, actor_id="user:x"
        )
        assert published.state == "published"
        assert integration.active_mapping_version_id == published.id
        version_one = published.id

    # A published mapping cannot change — not its definition, not its
    # target schema.
    with pytest.raises(ImmutableVersionError):
        async with db.session_scope() as session:
            row = await MappingProfileVersionRepository(session, CONTEXT).get(version_one)
            assert row is not None
            row.definition = {"fields": []}
            await session.flush()

    async with db.session_scope() as session:
        integration = await IntegrationRepository(session, CONTEXT).get(integration_id)
        assert integration is not None
        second = await create_mapping_draft(
            session,
            CONTEXT,
            integration=integration,
            definition=MAPPING,
            target_schema=TARGET,
            actor_id="user:x",
        )
        assert second.version_number == 2
        # Publishing a non-draft is refused.
        first_row = await MappingProfileVersionRepository(session, CONTEXT).get(version_one)
        assert first_row is not None
        with pytest.raises(InvalidVersionStateError):
            await publish_mapping_draft(
                session, CONTEXT, integration=integration, draft=first_row, actor_id="user:x"
            )
        await publish_mapping_draft(
            session, CONTEXT, integration=integration, draft=second, actor_id="user:x"
        )
        versions = await MappingProfileVersionRepository(session, CONTEXT).list_for_integration(
            integration.id
        )
        assert [(v.version_number, v.state) for v in versions] == [
            (1, "superseded"),
            (2, "published"),
        ]
        assert integration.active_mapping_version_id == second.id


async def test_everything_is_tenant_scoped(db: DatabaseSessions) -> None:
    integration_id = await make_integration(db)
    async with db.session_scope() as session:
        other = OrganizationContext(organization_id=ORG_B)
        assert await IntegrationRepository(session, other).get(integration_id) is None
        assert await IntegrationRepository(session, other).get_by_slug("northstar-erp") is None

    from soa_db.tenant_guard import RLS_PROTECTED_TABLES

    for table in ("integrations", "integration_credentials", "mapping_profile_versions"):
        assert table in RLS_PROTECTED_TABLES
