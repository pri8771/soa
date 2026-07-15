"""Shared document intake pipeline (ING-007/013).

Every ingestion channel — upload sessions, the public API, email later —
funnels through ``finalize_document_intake`` once the bytes are stored:
document + original artifact created atomically, signature inspection,
malware scan (fail closed), duplicate policy, and — only when the
document reaches ``queued`` — the exactly-once preprocess job and
outbox event. One pipeline, one set of rules, regardless of the door
the file came in through.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.services.duplicates import (
    DuplicatePolicy,
    find_exact_duplicate,
    get_duplicate_policy,
    lock_exact_duplicate_intake,
    mark_duplicate,
)
from soa_api.services.file_inspection import InspectionResult, InspectionVerdict, inspect_file
from soa_api.services.malware import MalwareScanner, ScanResult, ScanVerdict
from soa_api.services.runtime_pins import resolve_runtime_pins
from soa_db.artifacts import ArtifactKind, create_artifact
from soa_db.audit import ActorType
from soa_db.documents import (
    Document,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.jobs import enqueue_job
from soa_db.outbox import enqueue_event
from soa_db.repository import OrganizationContext


@dataclass(frozen=True)
class IntakeDeclaration:
    stream_id: uuid.UUID
    stream_config: dict[str, Any]
    source_channel: SourceChannel
    filename: str
    content_type: str
    sha256: str
    size_bytes: int
    object_key: str
    client_reference: str | None = None
    source_metadata: dict[str, Any] | None = None
    document_id: uuid.UUID | None = None
    priority: int = 100
    #: Configuration snapshot for the run (PRC-003): pinned at enqueue
    #: time so the orchestrator never re-resolves configuration mid-run.
    stream_version_id: uuid.UUID | None = None
    config_fingerprint: str | None = None


@dataclass(frozen=True)
class IntakeSafetyResult:
    """Content checks that may run without a database transaction."""

    inspection: InspectionResult
    scan: ScanResult | None


async def evaluate_intake_safety(
    declaration: IntakeDeclaration,
    data: bytes,
    scanner: MalwareScanner,
) -> IntakeSafetyResult:
    inspection = inspect_file(
        head=data[:64],
        declared_type=declaration.content_type,
        filename=declaration.filename,
    )
    scan = await scanner.scan(data) if inspection.verdict is InspectionVerdict.PASSED else None
    return IntakeSafetyResult(inspection=inspection, scan=scan)


async def finalize_document_intake(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    declaration: IntakeDeclaration,
    safety: IntakeSafetyResult,
    actor_id: str,
    actor_type: ActorType = ActorType.USER,
) -> Document:
    """Register bytes whose external safety checks have already completed.

    Requiring ``safety`` keeps malware-scanner network I/O out of this SQL
    unit of work by construction. Callers must evaluate the bytes before
    opening the registration transaction.
    """
    # PostgreSQL holds this transaction-scoped fence through the caller's
    # commit. Acquiring it before any registration work guarantees that the
    # later duplicate lookup sees a concurrent winner in this tenant/stream.
    await lock_exact_duplicate_intake(
        session,
        context,
        stream_id=declaration.stream_id,
        content_sha256=declaration.sha256,
    )
    pins = await resolve_runtime_pins(
        session,
        context,
        stream_version_id=declaration.stream_version_id,
    )
    document = await create_document(
        session,
        context,
        document_id=declaration.document_id,
        stream_id=declaration.stream_id,
        source_channel=declaration.source_channel,
        original_filename=declaration.filename,
        content_sha256=declaration.sha256,
        size_bytes=declaration.size_bytes,
        content_type=declaration.content_type,
        client_reference=declaration.client_reference,
        priority=declaration.priority,
        source_metadata=declaration.source_metadata,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    await create_artifact(
        session,
        context,
        document_id=document.id,
        kind=ArtifactKind.ORIGINAL,
        object_key=declaration.object_key,
        sha256=declaration.sha256,
        size_bytes=declaration.size_bytes,
        content_type=declaration.content_type,
        produced_by_stage="intake",
        actor_type=actor_type,
        actor_id=actor_id,
    )

    await transition_document(
        session,
        context,
        document=document,
        to_state=DocumentState.VALIDATING_FILE,
        actor_id=actor_id,
        actor_type=actor_type,
    )
    inspection = safety.inspection
    if inspection.verdict is not InspectionVerdict.PASSED:
        outcome = {
            InspectionVerdict.REJECTED: DocumentState.REJECTED,
            InspectionVerdict.QUARANTINED: DocumentState.QUARANTINED,
        }[inspection.verdict]
        return await transition_document(
            session,
            context,
            document=document,
            to_state=outcome,
            reason=inspection.reason,
            actor_id="system:file-inspection",
        )

    scan = safety.scan
    if scan is None:
        raise RuntimeError("a passed file inspection requires a malware scan result")
    if scan.verdict is ScanVerdict.INFECTED:
        return await transition_document(
            session,
            context,
            document=document,
            to_state=DocumentState.QUARANTINED,
            reason=f"malware detected: {scan.detail}",
            actor_id="system:malware-scan",
        )
    if scan.verdict is ScanVerdict.ERROR:
        return await transition_document(
            session,
            context,
            document=document,
            to_state=DocumentState.FAILED_RETRYABLE,
            reason=f"malware scan unavailable: {scan.detail}",
            actor_id="system:malware-scan",
        )

    duplicate = await find_exact_duplicate(
        session,
        context,
        stream_id=declaration.stream_id,
        content_sha256=declaration.sha256,
        exclude_document_id=document.id,
    )
    policy = get_duplicate_policy(pins.config)
    if duplicate is not None:
        await mark_duplicate(
            session,
            context,
            document=document,
            original=duplicate,
            policy=policy,
            actor_id="system:duplicate-detection",
        )
        if policy is DuplicatePolicy.REJECT:
            return await transition_document(
                session,
                context,
                document=document,
                to_state=DocumentState.REJECTED,
                reason=f"exact duplicate of document {duplicate.id}",
                actor_id="system:duplicate-detection",
            )

    await transition_document(
        session,
        context,
        document=document,
        to_state=DocumentState.QUEUED,
        actor_id="system:malware-scan",
    )
    await enqueue_job(
        session,
        job_type="document.preprocess",
        payload={
            "document_id": str(document.id),
            "stream_id": str(declaration.stream_id),
            "organization_id": str(context.organization_id),
            **pins.job_payload(),
        },
        organization_id=context.organization_id,
        dedupe_key=f"document.preprocess:{document.id}",
        priority=document.priority,
    )
    await enqueue_event(
        session,
        event_type="document.registered",
        payload={
            "document_id": str(document.id),
            "stream_id": str(declaration.stream_id),
            "source_channel": document.source_channel,
            "content_sha256": document.content_sha256,
        },
        organization_id=context.organization_id,
        dedupe_key=f"document.registered:{document.id}",
    )
    return document
