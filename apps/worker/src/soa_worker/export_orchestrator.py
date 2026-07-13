"""Export execution (EXP-008, worker side).

Runs one export job end to end: load the APPROVED canonical payload
(the immutable CAN-003 row — nothing is ever re-extracted or re-mapped
from raw data), execute the PINNED mapping version, store the export
artifact, deliver the signed webhook, record the attempt, and move the
job and the document.

Exactly-once business intent: a job that already settled
(succeeded/cancelled/failed_terminal) is a no-op — no new attempt, no
new request. Retries and operator replays re-enter through the state
machine and always carry the same business key and the same payload.

The webhook body IS the stored JSON export artifact byte for byte, so
what an operator downloads is exactly what the receiver was sent.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

import soa_worker.webhook_adapter  # noqa: F401  (registers the "webhook" adapter)
from soa_canonical.export_encoding import ExportMetadata
from soa_canonical.mapping_engine import (
    MappingDefinitionError,
    MappingExecutionError,
    execute_mapping,
)
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
    IntegrationRepository,
    MappingProfileVersionRepository,
    credential_secret_for_delivery,
)
from soa_db.repository import OrganizationContext
from soa_storage import ObjectStore
from soa_worker.erp_adapter import (
    AdapterDeliveryRequest,
    UnknownAdapterError,
    resolve_adapter,
)
from soa_worker.export_artifacts import store_export_artifact

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
    return ExportExecutionResult(outcome="terminal_error", job_state=job.state, detail=reason)


async def execute_export(
    session: AsyncSession,
    store: ObjectStore,
    context: OrganizationContext,
    *,
    export_job_id: uuid.UUID,
    client: httpx.AsyncClient,
    allowlist: Sequence[str],
    timestamp: int,
    resolve: Callable[[str], list[str]] | None = None,
) -> ExportExecutionResult:
    """One delivery pass over an export job. ``timestamp`` is the unix
    time the caller stamps on the signature (injected for determinism)."""
    job = await ExportJobRepository(session, context).get(export_job_id)
    if job is None:
        raise ValueError(f"export job {export_job_id} does not exist in this organization")
    if job.state in _SETTLED:
        # Exactly-once: settled intent is never re-delivered implicitly.
        return ExportExecutionResult(outcome="skipped", job_state=job.state)

    document = await DocumentRepository(session, context).get(job.document_id)
    integration = await IntegrationRepository(session, context).get(job.integration_id)
    mapping = await MappingProfileVersionRepository(session, context).get(job.mapping_version_id)
    payload_row = await CanonicalPayloadRepository(session, context).get(job.canonical_payload_id)

    await transition_export_job(
        session, context, job=job, to_state=ExportJobState.IN_PROGRESS, actor_id=ACTOR
    )
    if document is not None and document.state == DocumentState.APPROVED.value:
        await transition_document(
            session,
            context,
            document=document,
            to_state=DocumentState.EXPORTING,
            reason=None,
            actor_id=ACTOR,
        )

    if integration is None or mapping is None or payload_row is None:
        return await _fail_terminal(
            session,
            context,
            job=job,
            document=document,
            reason="export prerequisites are gone (integration, mapping, or payload)",
        )
    secret = await credential_secret_for_delivery(session, context, integration=integration)
    if not integration.endpoint_url or secret is None:
        return await _fail_terminal(
            session,
            context,
            job=job,
            document=document,
            reason="integration has no endpoint or no live credential",
        )

    # Map the STORED approved payload under the PINNED mapping version.
    try:
        mapped = execute_mapping(
            mapping.definition, payload_row.payload, target_schema=mapping.target_schema or None
        )
    except (MappingDefinitionError, MappingExecutionError) as error:
        return await _fail_terminal(
            session, context, job=job, document=document, reason=str(error)[:500]
        )

    metadata = ExportMetadata(
        schema_version=payload_row.schema_version,
        mapping_version_number=mapping.version_number,
        integration_slug=integration.slug,
        business_key=job.business_key,
        document_id=str(job.document_id),
        run_id=str(job.run_id),
        exported_at=datetime.fromtimestamp(timestamp, tz=UTC).isoformat(),
    )
    stored = await store_export_artifact(
        session,
        store,
        context,
        job=job,
        export_format="json",
        payload=mapped.payload,
        metadata=metadata,
    )
    body = await store.get(stored.artifact.object_key)

    # Delivery goes through the ADAPTER CONTRACT (EXP-010): resolve by
    # integration type — orchestration never special-cases a destination.
    try:
        adapter = resolve_adapter(integration.integration_type)
    except UnknownAdapterError as unknown:
        return await _fail_terminal(
            session, context, job=job, document=document, reason=str(unknown)[:500]
        )
    webhook = await adapter.deliver(
        client,
        AdapterDeliveryRequest(
            url=integration.endpoint_url,
            body=body,
            secret=secret,
            business_key=job.business_key,
            attempt_number=job.attempt_count + 1,
            timestamp=timestamp,
            allowlist=allowlist,
            resolve=resolve,
        ),
    )
    attempt = await record_delivery_attempt(
        session,
        context,
        job=job,
        outcome=webhook.outcome,
        response_status=webhook.response_status,
        safe_error=webhook.safe_error,
        request_sha256=stored.artifact.sha256,
    )

    if webhook.outcome == "delivered":
        await transition_export_job(
            session, context, job=job, to_state=ExportJobState.SUCCEEDED, actor_id=ACTOR
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
    elif webhook.outcome == "retryable_error":
        await transition_export_job(
            session,
            context,
            job=job,
            to_state=ExportJobState.FAILED_RETRYABLE,
            actor_id=ACTOR,
            reason=webhook.safe_error,
        )
    else:
        return await _fail_terminal(
            session,
            context,
            job=job,
            document=document,
            reason=webhook.safe_error or "receiver rejected the delivery",
        )

    return ExportExecutionResult(
        outcome=webhook.outcome,
        job_state=job.state,
        attempt_number=attempt.attempt_number,
        detail=webhook.safe_error,
    )
