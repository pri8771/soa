"""Upload-session endpoints (ING-002).

POST /orgs/{slug}/streams/{stream}/uploads   declare + get signed target
POST /orgs/{slug}/uploads/{id}/complete      verify object, register doc
POST /orgs/{slug}/uploads/{id}/abort         discard the session

Policy (type/size/quota) is validated before signing; completion
verifies the stored object's size and SHA-256 against the declaration
and registers the document exactly once (retrying complete after a
network failure returns the same document instead of a second one).
Sessions expire lazily: a touch after the deadline flips them to
expired and answers 410.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import (
    DbSession,
    Dependencies,
    MalwareScannerDep,
    ObjectStoreDep,
    get_dependencies,
)
from soa_api.domain.streams import StreamRepository, StreamStatus, StreamVersionRepository
from soa_api.domain.uploads import (
    UploadPolicyError,
    UploadSession,
    UploadSessionRepository,
    UploadSessionState,
    create_upload_session,
    is_expired,
    validate_upload_declaration,
)
from soa_api.services.duplicates import (
    DuplicatePolicy,
    find_exact_duplicate,
    get_duplicate_policy,
    mark_duplicate,
)
from soa_api.services.file_inspection import InspectionVerdict, inspect_file
from soa_api.services.file_limits import (
    FileLimits,
    LimitViolation,
    check_size,
    record_limit_violation,
    resolve_limits,
)
from soa_api.services.malware import ScanVerdict
from soa_db.artifacts import ArtifactKind, create_artifact
from soa_db.audit import ActorType, record_audit_event
from soa_db.documents import (
    DocumentRepository,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.jobs import enqueue_job
from soa_db.outbox import enqueue_event
from soa_db.types import utcnow, uuid7
from soa_storage import ObjectNotFoundError
from soa_storage.keys import artifact_key

router = APIRouter(tags=["uploads"])

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class UploadCreateRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    client_reference: str | None = Field(default=None, min_length=1, max_length=200)


class UploadSessionResponse(BaseModel):
    session_id: str
    document_id: str
    upload_url: str
    upload_method: str
    expires_at: str
    state: str

    @classmethod
    def from_model(cls, record: UploadSession, *, url: str, method: str) -> "UploadSessionResponse":
        return cls(
            session_id=str(record.id),
            document_id=str(record.document_id),
            upload_url=url,
            upload_method=method,
            expires_at=record.expires_at.isoformat(),
            state=record.state,
        )


class CompleteResponse(BaseModel):
    document_id: str
    state: str


def _actor(authorized: AuthorizedContext) -> str:
    return f"user:{authorized.membership.user_id}"


async def _stream_config_by_id(
    session: DbSession, authorized: AuthorizedContext, stream_id: uuid.UUID
) -> dict[str, object]:
    """The stream's published resolved configuration, or {}."""
    stream = await StreamRepository(session, authorized.org_context).get(stream_id)
    if stream is None or stream.active_version_id is None:
        return {}
    active = await StreamVersionRepository(session, authorized.org_context).get(
        stream.active_version_id
    )
    if active is None or not active.resolved_snapshot:
        return {}
    config = active.resolved_snapshot.get("config")
    return dict(config) if isinstance(config, dict) else {}


@router.post(
    "/orgs/{organization_slug}/streams/{stream_slug}/uploads",
    status_code=status.HTTP_201_CREATED,
)
async def create_upload(
    stream_slug: str,
    body: UploadCreateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.upload"))],
    session: DbSession,
    store: ObjectStoreDep,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> UploadSessionResponse:
    stream = await StreamRepository(session, authorized.org_context).get_by_slug(stream_slug)
    if stream is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stream not found.")
    if stream.status == StreamStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This stream is archived and no longer accepts documents.",
        )

    # Effective limits (ING-005): the stream's published configuration may
    # LOWER platform maxima, never raise them.
    platform_limits = FileLimits(
        max_size_bytes=deps.settings.max_upload_bytes,
        max_pages=deps.settings.max_pages_per_document,
        max_total_pixels=deps.settings.max_total_pixels,
        max_decompressed_bytes=deps.settings.max_decompressed_bytes,
        max_conversion_seconds=deps.settings.max_conversion_seconds,
    )
    stream_config = await _stream_config_by_id(session, authorized, stream.id)
    limits = resolve_limits(platform_limits, stream_config)

    repo = UploadSessionRepository(session, authorized.org_context)
    try:
        check_size(body.size_bytes, limits)
        validate_upload_declaration(
            content_type=body.content_type,
            size_bytes=body.size_bytes,
            pending_sessions=await repo.count_pending(),
            max_size_bytes=limits.max_size_bytes,
            max_pending_sessions=deps.settings.max_pending_upload_sessions,
        )
    except LimitViolation as exc:
        # Limit violations are auditable (ING-005). The 422 rolls the
        # request transaction back, so the audit event gets its own.
        if deps.db is not None:
            async with deps.db.session_scope() as audit_session:
                await record_limit_violation(
                    audit_session,
                    authorized.org_context,
                    violation=exc,
                    target_type="stream",
                    target_id=stream.id,
                    actor_id=_actor(authorized),
                    actor_type=ActorType.USER,
                )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    except UploadPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None

    # A duplicate client reference is answered with the existing document,
    # never a second delivery.
    if body.client_reference is not None:
        existing = await DocumentRepository(
            session, authorized.org_context
        ).get_by_client_reference(stream.id, body.client_reference)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"client_reference {body.client_reference!r} already registered "
                    f"document {existing.id}"
                ),
            )

    document_id = uuid7()
    key = artifact_key(
        authorized.org_context.organization_id,
        document_id,
        kind="original",
        filename=body.filename,
    )
    record = await create_upload_session(
        session,
        authorized.org_context,
        stream_id=stream.id,
        document_id=document_id,
        object_key=key,
        filename=body.filename,
        content_type=body.content_type,
        size_bytes=body.size_bytes,
        sha256=body.sha256,
        client_reference=body.client_reference,
        ttl_seconds=deps.settings.upload_session_ttl_seconds,
        actor_id=_actor(authorized),
    )
    signed = await store.signed_upload_url(
        key,
        expires_in_seconds=deps.settings.upload_session_ttl_seconds,
        content_type=body.content_type,
    )
    return UploadSessionResponse.from_model(record, url=signed.url, method=signed.method)


async def _load_session(
    session: DbSession, authorized: AuthorizedContext, session_id: uuid.UUID
) -> UploadSession:
    record = await UploadSessionRepository(session, authorized.org_context).get(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found."
        )
    if record.state == UploadSessionState.PENDING.value and is_expired(record):
        record.state = UploadSessionState.EXPIRED.value
        await session.flush()
    if record.state == UploadSessionState.EXPIRED.value:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="This upload session has expired."
        )
    return record


@router.post("/orgs/{organization_slug}/uploads/{session_id}/complete")
async def complete_upload(
    session_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.upload"))],
    session: DbSession,
    store: ObjectStoreDep,
    scanner: MalwareScannerDep,
) -> CompleteResponse:
    record = await _load_session(session, authorized, session_id)
    documents = DocumentRepository(session, authorized.org_context)

    if record.state == UploadSessionState.COMPLETED.value:
        # Idempotent retry: the document exists; report it, create nothing.
        existing = await documents.get(record.document_id)
        if existing is None:  # metadata drift — should be impossible
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Session is completed but its document is missing.",
            )
        return CompleteResponse(document_id=str(existing.id), state=existing.state)
    if record.state == UploadSessionState.ABORTED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This upload session was aborted."
        )

    # Verify the stored object against the declaration.
    try:
        metadata = await store.head(record.object_key)
    except ObjectNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No object has been uploaded for this session yet.",
        ) from None
    problems: list[str] = []
    if metadata.size != record.declared_size_bytes:
        problems.append(
            f"size mismatch: declared {record.declared_size_bytes}, stored {metadata.size}"
        )
    if metadata.sha256 and metadata.sha256 != record.declared_sha256:
        problems.append(
            f"sha256 mismatch: declared {record.declared_sha256[:12]}…, "
            f"stored {metadata.sha256[:12]}…"
        )
    if problems:
        # The session stays pending: the client may re-upload the correct
        # bytes to the same signed target and complete again.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="; ".join(problems)
        )

    document = await create_document(
        session,
        authorized.org_context,
        document_id=record.document_id,
        stream_id=record.stream_id,
        source_channel=SourceChannel.UPLOAD,
        original_filename=record.declared_filename,
        content_sha256=record.declared_sha256,
        size_bytes=record.declared_size_bytes,
        content_type=record.declared_content_type,
        client_reference=record.client_reference,
        source_metadata={"uploader": _actor(authorized)},
        actor_id=_actor(authorized),
    )
    await create_artifact(
        session,
        authorized.org_context,
        document_id=record.document_id,
        kind=ArtifactKind.ORIGINAL,
        object_key=record.object_key,
        sha256=record.declared_sha256,
        size_bytes=record.declared_size_bytes,
        content_type=record.declared_content_type,
        produced_by_stage="intake",
        actor_type=ActorType.USER,
        actor_id=_actor(authorized),
    )
    record.state = UploadSessionState.COMPLETED.value
    record.completed_at = utcnow()
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=_actor(authorized),
        action="upload_session.completed",
        target_type="upload_session",
        target_id=str(record.id),
        organization_id=authorized.org_context.organization_id,
        summary={"document_id": str(record.document_id)},
    )

    # File-safety inspection (ING-003): trust the bytes, not the label.
    # Runs synchronously here until ING-004/007 move it onto worker jobs.
    await transition_document(
        session,
        authorized.org_context,
        document=document,
        to_state=DocumentState.VALIDATING_FILE,
        actor_id=_actor(authorized),
        actor_type=ActorType.USER,
    )
    data = await store.get(record.object_key)
    inspection = inspect_file(
        head=data[:64],
        declared_type=record.declared_content_type,
        filename=record.declared_filename,
    )
    if inspection.verdict is not InspectionVerdict.PASSED:
        outcome = {
            InspectionVerdict.REJECTED: DocumentState.REJECTED,
            InspectionVerdict.QUARANTINED: DocumentState.QUARANTINED,
        }[inspection.verdict]
        await transition_document(
            session,
            authorized.org_context,
            document=document,
            to_state=outcome,
            reason=inspection.reason,
            actor_id="system:file-inspection",
        )
        return CompleteResponse(document_id=str(record.document_id), state=document.state)

    # Malware scan (ING-004): unscanned files never proceed. A scanner
    # outage parks the document as failed_retryable — fail closed, retry
    # later; it does NOT pass unscanned.
    scan = await scanner.scan(data)
    if scan.verdict is ScanVerdict.INFECTED:
        await transition_document(
            session,
            authorized.org_context,
            document=document,
            to_state=DocumentState.QUARANTINED,
            reason=f"malware detected: {scan.detail}",
            actor_id="system:malware-scan",
        )
    elif scan.verdict is ScanVerdict.ERROR:
        await transition_document(
            session,
            authorized.org_context,
            document=document,
            to_state=DocumentState.FAILED_RETRYABLE,
            reason=f"malware scan unavailable: {scan.detail}",
            actor_id="system:malware-scan",
        )
    else:
        # Exact-duplicate policy (ING-006): duplicates are never silent.
        duplicate = await find_exact_duplicate(
            session,
            authorized.org_context,
            stream_id=record.stream_id,
            content_sha256=record.declared_sha256,
            exclude_document_id=document.id,
        )
        policy = get_duplicate_policy(
            await _stream_config_by_id(session, authorized, record.stream_id)
        )
        if duplicate is not None:
            await mark_duplicate(
                session,
                authorized.org_context,
                document=document,
                original=duplicate,
                policy=policy,
                actor_id="system:duplicate-detection",
            )
        if duplicate is not None and policy is DuplicatePolicy.REJECT:
            await transition_document(
                session,
                authorized.org_context,
                document=document,
                to_state=DocumentState.REJECTED,
                reason=f"exact duplicate of document {duplicate.id}",
                actor_id="system:duplicate-detection",
            )
        else:
            await transition_document(
                session,
                authorized.org_context,
                document=document,
                to_state=DocumentState.QUEUED,
                actor_id="system:malware-scan",
            )
            # Registration is atomic (ING-007): the processing job and the
            # outbox event commit with the document/artifact/audit rows or
            # not at all, and dedupe keys make both exactly-once even
            # under concurrent completes.
            await enqueue_job(
                session,
                job_type="document.preprocess",
                payload={
                    "document_id": str(document.id),
                    "stream_id": str(record.stream_id),
                    "organization_id": str(authorized.org_context.organization_id),
                },
                organization_id=authorized.org_context.organization_id,
                dedupe_key=f"document.preprocess:{document.id}",
                priority=document.priority,
            )
            await enqueue_event(
                session,
                event_type="document.registered",
                payload={
                    "document_id": str(document.id),
                    "stream_id": str(record.stream_id),
                    "source_channel": document.source_channel,
                    "content_sha256": document.content_sha256,
                },
                organization_id=authorized.org_context.organization_id,
                dedupe_key=f"document.registered:{document.id}",
            )
    return CompleteResponse(document_id=str(record.document_id), state=document.state)


@router.post("/orgs/{organization_slug}/uploads/{session_id}/abort")
async def abort_upload(
    session_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.upload"))],
    session: DbSession,
    store: ObjectStoreDep,
) -> dict[str, str]:
    record = await _load_session(session, authorized, session_id)
    if record.state == UploadSessionState.COMPLETED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Completed sessions cannot be aborted; the document already exists.",
        )
    if record.state == UploadSessionState.PENDING.value:
        record.state = UploadSessionState.ABORTED.value
        await session.flush()
        # Discard any bytes the client already sent.
        try:
            await store.delete(record.object_key)
        except ObjectNotFoundError:
            pass
        await record_audit_event(
            session,
            actor_type=ActorType.USER,
            actor_id=_actor(authorized),
            action="upload_session.aborted",
            target_type="upload_session",
            target_id=str(record.id),
            organization_id=authorized.org_context.organization_id,
            summary={},
        )
    return {"state": record.state}
