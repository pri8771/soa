"""Durable, idempotent post-commit revocation of managed secret values."""

import uuid

from sqlalchemy import select

from soa_config import SecretReference, SecretStore
from soa_db import DatabaseSessions
from soa_db.integrations import (
    SECRET_REVOKE_JOB_TYPE,
    Integration,
    IntegrationCredentialRepository,
)
from soa_db.provider_credentials import ProviderCredentialRepository
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_worker.registry import HandlerRegistry, JobEnvelope


def register_secret_revoke_handler(
    registry: HandlerRegistry,
    db: DatabaseSessions,
    secret_store: SecretStore,
) -> None:
    """Register the job that removes a revoked external value.

    The queue row's organization id is trusted metadata. Before touching
    the secret store, the handler proves that the bounded credential type
    belongs to that organization, is revoked, and still names the queued
    reference. Integration credentials additionally cannot remain a live
    integration pointer. Re-delivery is safe because every secret-store
    ``revoke`` implementation is idempotent.
    """

    @registry.register(SECRET_REVOKE_JOB_TYPE)
    async def revoke_secret(job: JobEnvelope) -> None:
        if job.organization_id is None:
            raise ValueError("secret.revoke requires a trusted organization id")
        raw_credential_id = job.payload.get("credential_id")
        raw_reference = job.payload.get("secret_reference")
        credential_type = job.payload.get("credential_type", "integration")
        if not isinstance(raw_credential_id, str) or not isinstance(raw_reference, str):
            raise ValueError("secret.revoke payload is invalid")
        if credential_type not in {"integration", "provider"}:
            raise ValueError("secret.revoke credential_type is invalid")
        credential_id = uuid.UUID(raw_credential_id)
        reference = SecretReference.parse(raw_reference)
        context = OrganizationContext(organization_id=job.organization_id)

        async with db.session_scope() as session:
            await bind_tenant(session, job.organization_id)
            credential = (
                await IntegrationCredentialRepository(session, context).get(credential_id)
                if credential_type == "integration"
                else await ProviderCredentialRepository(session, context).get(credential_id)
            )
            if credential is None:
                raise ValueError("secret.revoke credential does not belong to the job tenant")
            if credential.revoked_at is None:
                raise ValueError("secret.revoke refused a credential that is still live")
            if credential.secret_reference != raw_reference:
                raise ValueError("secret.revoke reference does not match the credential")
            if credential_type == "integration":
                live_pointer = (
                    await session.execute(
                        select(Integration.id).where(
                            Integration.organization_id == job.organization_id,
                            Integration.credential_id == credential_id,
                        )
                    )
                ).scalar_one_or_none()
                if live_pointer is not None:
                    raise ValueError("secret.revoke refused a credential still referenced as live")

        await secret_store.revoke(reference)


__all__ = ["register_secret_revoke_handler"]
