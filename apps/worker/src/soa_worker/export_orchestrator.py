"""Durable export delivery with short database transactions.

One export intent pins an approved canonical payload, mapping version,
integration, and business idempotency key. Database work is split into a
prepare claim, immutable artifact registration, and result finalization; cloud
secret resolution, object storage, DNS/TLS, and receiver latency happen with
no SQL transaction or pool connection held.

The artifact body uses the export job's durable creation time, not an attempt
timestamp. Retries therefore send byte-identical content even on another day.
The webhook signature still uses the caller-supplied attempt timestamp.
"""

import uuid
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import soa_worker.erp_rest_adapters
import soa_worker.quickbooks_adapter
import soa_worker.webhook_adapter
from soa_canonical.export_encoding import ExportMetadata, encode_json_export
from soa_canonical.mapping_engine import (
    MappingDefinitionError,
    MappingExecutionError,
    execute_mapping,
)
from soa_config import SecretNotFoundError, SecretReference, SecretStore
from soa_db import DatabaseSessions
from soa_db.artifacts import Artifact, ArtifactKind, create_artifact
from soa_db.canonical_payloads import CanonicalPayloadRepository
from soa_db.documents import Document, DocumentRepository, DocumentState, transition_document
from soa_db.exports import (
    ExportJob,
    ExportJobRepository,
    ExportJobState,
    record_delivery_attempt,
    transition_export_job,
)
from soa_db.integrations import (
    IntegrationCredentialRepository,
    IntegrationRepository,
    IntegrationStatus,
    MappingProfileVersionRepository,
)
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_storage import ObjectStore, sha256_hex
from soa_storage.keys import artifact_key
from soa_worker.erp_adapter import (
    AdapterDeliveryRequest,
    UnknownAdapterError,
    resolve_adapter,
)
from soa_worker.export_artifacts import ExportArtifactMismatchError

# Importing the modules registers every shipped destination adapter.
_REGISTERED_ADAPTER_MODULES = (
    soa_worker.erp_rest_adapters,
    soa_worker.quickbooks_adapter,
    soa_worker.webhook_adapter,
)

ACTOR = "system:export"

_SETTLED = (
    ExportJobState.SUCCEEDED.value,
    ExportJobState.FAILED_TERMINAL.value,
    ExportJobState.CANCELLED.value,
)


@dataclass(frozen=True)
class ExportExecutionResult:
    outcome: str  # delivered | retryable_error | terminal_error | skipped
    job_state: str
    attempt_number: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _PreparedExport:
    job_id: uuid.UUID
    document_id: uuid.UUID
    run_id: uuid.UUID
    business_key: str
    expected_attempt_number: int
    job_created_at: datetime
    integration_slug: str
    integration_type: str
    endpoint_url: str
    credential_reference: SecretReference
    mapping_version_number: int
    mapping_definition: dict[str, Any]
    target_schema: dict[str, Any]
    schema_version: str
    canonical_payload: dict[str, Any]


async def _fail_terminal(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    job: ExportJob,
    document: Document | None,
    reason: str,
) -> ExportExecutionResult:
    await transition_export_job(
        session,
        context,
        job=job,
        to_state=ExportJobState.FAILED_TERMINAL,
        actor_id=ACTOR,
        reason=reason,
    )
    if document is not None and document.state == DocumentState.EXPORTING.value:
        await transition_document(
            session,
            context,
            document=document,
            to_state=DocumentState.FAILED_TERMINAL,
            reason=reason[:500],
            actor_id=ACTOR,
        )
    return ExportExecutionResult("terminal_error", job.state, detail=reason)


async def _prepare_export(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    export_job_id: uuid.UUID,
) -> _PreparedExport | ExportExecutionResult:
    job = await ExportJobRepository(session, context).get(export_job_id, for_update=True)
    if job is None:
        raise ValueError(f"export job {export_job_id} does not exist in this organization")
    if job.state in _SETTLED:
        return ExportExecutionResult("skipped", job.state)

    document = await DocumentRepository(session, context).get(job.document_id)
    integration = await IntegrationRepository(session, context).get(job.integration_id)
    mapping = await MappingProfileVersionRepository(session, context).get(job.mapping_version_id)
    payload = await CanonicalPayloadRepository(session, context).get(job.canonical_payload_id)

    if job.state != ExportJobState.IN_PROGRESS.value:
        await transition_export_job(
            session,
            context,
            job=job,
            to_state=ExportJobState.IN_PROGRESS,
            actor_id=ACTOR,
        )
    if document is not None and document.state == DocumentState.APPROVED.value:
        await transition_document(
            session,
            context,
            document=document,
            to_state=DocumentState.EXPORTING,
            actor_id=ACTOR,
        )

    if integration is None or mapping is None or payload is None:
        return await _fail_terminal(
            session,
            context,
            job=job,
            document=document,
            reason="export prerequisites are gone (integration, mapping, or payload)",
        )
    if integration.status != IntegrationStatus.ACTIVE:
        return await _fail_terminal(
            session,
            context,
            job=job,
            document=document,
            reason=f"integration is {integration.status}; only active integrations may deliver",
        )
    if not integration.endpoint_url or integration.credential_id is None:
        return await _fail_terminal(
            session,
            context,
            job=job,
            document=document,
            reason="integration has no endpoint or no live credential",
        )
    credential = await IntegrationCredentialRepository(session, context).get(
        integration.credential_id
    )
    if credential is None or credential.revoked_at is not None:
        return await _fail_terminal(
            session,
            context,
            job=job,
            document=document,
            reason="integration has no endpoint or no live credential",
        )
    try:
        credential_reference = SecretReference.parse(credential.secret_reference)
    except ValueError:
        return await _fail_terminal(
            session,
            context,
            job=job,
            document=document,
            reason="integration credential reference is invalid",
        )

    return _PreparedExport(
        job_id=job.id,
        document_id=job.document_id,
        run_id=job.run_id,
        business_key=job.business_key,
        expected_attempt_number=job.attempt_count + 1,
        job_created_at=job.created_at,
        integration_slug=integration.slug,
        integration_type=integration.integration_type,
        endpoint_url=integration.endpoint_url,
        credential_reference=credential_reference,
        mapping_version_number=mapping.version_number,
        mapping_definition=deepcopy(mapping.definition),
        target_schema=deepcopy(mapping.target_schema),
        schema_version=payload.schema_version,
        canonical_payload=deepcopy(payload.payload),
    )


async def _persist_terminal(
    db: DatabaseSessions,
    context: OrganizationContext,
    *,
    export_job_id: uuid.UUID,
    reason: str,
) -> ExportExecutionResult:
    async with db.session_scope() as session:
        await bind_tenant(session, context.organization_id)
        job = await ExportJobRepository(session, context).get(export_job_id, for_update=True)
        if job is None:
            raise ValueError("export job disappeared while recording a failure")
        if job.state == ExportJobState.SUCCEEDED.value:
            return ExportExecutionResult("skipped", job.state)
        if job.state == ExportJobState.FAILED_TERMINAL.value:
            return ExportExecutionResult("terminal_error", job.state, detail=job.last_error)
        document = await DocumentRepository(session, context).get(job.document_id)
        return await _fail_terminal(
            session,
            context,
            job=job,
            document=document,
            reason=reason[:500],
        )


async def _persist_export_artifact(
    db: DatabaseSessions,
    context: OrganizationContext,
    prepared: _PreparedExport,
    *,
    body: bytes,
) -> tuple[str, str]:
    """Register one stable object coordinate before storage I/O.

    If storage fails after this short commit, retry reconstructs the same body
    from immutable pins, finds the same row, and repairs the same object key.
    """

    digest = sha256_hex(body)
    stage = f"export:{prepared.job_id}:json"
    async with db.session_scope() as session:
        await bind_tenant(session, context.organization_id)
        existing = (
            (
                await session.execute(
                    select(Artifact).where(
                        Artifact.organization_id == context.organization_id,
                        Artifact.document_id == prepared.document_id,
                        Artifact.kind == ArtifactKind.EXPORT_PAYLOAD.value,
                        Artifact.produced_by_stage == stage,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            if existing.sha256 != digest or existing.size_bytes != len(body):
                raise ExportArtifactMismatchError(prepared.job_id, "json")
            return existing.object_key, digest
        key = artifact_key(
            context.organization_id,
            prepared.document_id,
            kind="export_payload",
            filename=f"{prepared.business_key.replace(':', '-')}.json",
        )
        await create_artifact(
            session,
            context,
            document_id=prepared.document_id,
            kind=ArtifactKind.EXPORT_PAYLOAD,
            object_key=key,
            sha256=digest,
            size_bytes=len(body),
            content_type="application/json",
            produced_by_run_id=prepared.run_id,
            produced_by_stage=stage,
            actor_id=ACTOR,
        )
        return key, digest


async def _finalize_delivery(
    db: DatabaseSessions,
    context: OrganizationContext,
    prepared: _PreparedExport,
    *,
    outcome: str,
    response_status: int | None,
    safe_error: str | None,
    request_sha256: str,
) -> ExportExecutionResult:
    async with db.session_scope() as session:
        await bind_tenant(session, context.organization_id)
        job = await ExportJobRepository(session, context).get(prepared.job_id, for_update=True)
        if job is None:
            raise ValueError("export job disappeared during delivery")
        if job.state in _SETTLED:
            return ExportExecutionResult("skipped", job.state)
        if (
            job.state != ExportJobState.IN_PROGRESS.value
            or job.attempt_count + 1 != prepared.expected_attempt_number
        ):
            raise ValueError("export delivery claim is stale")
        document = await DocumentRepository(session, context).get(job.document_id)
        attempt = await record_delivery_attempt(
            session,
            context,
            job=job,
            outcome=outcome,
            response_status=response_status,
            safe_error=safe_error,
            request_sha256=request_sha256,
        )
        if outcome == "delivered":
            await transition_export_job(
                session,
                context,
                job=job,
                to_state=ExportJobState.SUCCEEDED,
                actor_id=ACTOR,
            )
            if document is not None and document.state == DocumentState.EXPORTING.value:
                await transition_document(
                    session,
                    context,
                    document=document,
                    to_state=DocumentState.COMPLETED,
                    reason="export delivered",
                    actor_id=ACTOR,
                )
        elif outcome == "retryable_error":
            await transition_export_job(
                session,
                context,
                job=job,
                to_state=ExportJobState.FAILED_RETRYABLE,
                actor_id=ACTOR,
                reason=safe_error,
            )
        else:
            failed = await _fail_terminal(
                session,
                context,
                job=job,
                document=document,
                reason=safe_error or "receiver rejected the delivery",
            )
            return ExportExecutionResult(
                failed.outcome,
                failed.job_state,
                attempt_number=attempt.attempt_number,
                detail=failed.detail,
            )
        return ExportExecutionResult(
            outcome,
            job.state,
            attempt_number=attempt.attempt_number,
            detail=safe_error,
        )


async def execute_export(
    db: DatabaseSessions,
    store: ObjectStore,
    context: OrganizationContext,
    *,
    export_job_id: uuid.UUID,
    client: httpx.AsyncClient,
    allowlist: Sequence[str],
    timestamp: int,
    secret_store: SecretStore,
    resolve: Callable[[str], list[str]] | None = None,
) -> ExportExecutionResult:
    """Execute one delivery pass without holding SQL across external I/O."""

    async with db.session_scope() as session:
        await bind_tenant(session, context.organization_id)
        prepared = await _prepare_export(session, context, export_job_id=export_job_id)
    if isinstance(prepared, ExportExecutionResult):
        return prepared

    try:
        secret = await secret_store.resolve(prepared.credential_reference)
    except SecretNotFoundError:
        return await _persist_terminal(
            db,
            context,
            export_job_id=prepared.job_id,
            reason="integration credential is missing or revoked; it must be re-set",
        )

    try:
        mapped = execute_mapping(
            prepared.mapping_definition,
            prepared.canonical_payload,
            target_schema=prepared.target_schema or None,
        )
    except (MappingDefinitionError, MappingExecutionError) as error:
        return await _persist_terminal(
            db,
            context,
            export_job_id=prepared.job_id,
            reason=str(error)[:500],
        )

    try:
        adapter = resolve_adapter(prepared.integration_type)
    except UnknownAdapterError as error:
        return await _persist_terminal(
            db,
            context,
            export_job_id=prepared.job_id,
            reason=str(error)[:500],
        )

    metadata = ExportMetadata(
        schema_version=prepared.schema_version,
        mapping_version_number=prepared.mapping_version_number,
        integration_slug=prepared.integration_slug,
        business_key=prepared.business_key,
        document_id=str(prepared.document_id),
        run_id=str(prepared.run_id),
        exported_at=prepared.job_created_at.isoformat(),
    )
    body = encode_json_export(mapped.payload, metadata)
    try:
        object_key, digest = await _persist_export_artifact(
            db,
            context,
            prepared,
            body=body,
        )
    except ExportArtifactMismatchError as error:
        return await _persist_terminal(
            db,
            context,
            export_job_id=prepared.job_id,
            reason=str(error)[:500],
        )
    stored = await store.put(
        object_key,
        body,
        content_type="application/json",
        sha256=digest,
    )
    if stored.sha256 != digest or stored.size != len(body):
        raise RuntimeError("object storage did not preserve the export artifact")

    delivery = await adapter.deliver(
        client,
        AdapterDeliveryRequest(
            url=prepared.endpoint_url,
            body=body,
            secret=secret,
            business_key=prepared.business_key,
            attempt_number=prepared.expected_attempt_number,
            timestamp=timestamp,
            allowlist=allowlist,
            resolve=resolve,
        ),
    )
    return await _finalize_delivery(
        db,
        context,
        prepared,
        outcome=delivery.outcome,
        response_status=delivery.response_status,
        safe_error=delivery.safe_error,
        request_sha256=digest,
    )
