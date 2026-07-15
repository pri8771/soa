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

import logging
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
from soa_api.domain.streams import StreamRepository, StreamStatus
from soa_api.domain.uploads import (
    UPLOAD_CLEANUP_GRACE,
    UPLOAD_CLEANUP_JOB_TYPE,
    UPLOAD_CLEANUP_SWEEP_GRACE,
    UPLOAD_VERIFICATION_LEASE,
    UploadPolicyError,
    UploadSession,
    UploadSessionRepository,
    UploadSessionState,
    create_upload_session,
    is_expired,
    validate_upload_declaration,
)
from soa_api.services.file_limits import (
    FileLimits,
    LimitViolation,
    check_size,
    record_limit_violation,
    resolve_limits,
)
from soa_api.services.ingestion import (
    IntakeDeclaration,
    evaluate_intake_safety,
    finalize_document_intake,
)
from soa_api.services.runtime_pins import RuntimePinError, RuntimePins, resolve_runtime_pins
from soa_db.audit import ActorType, record_audit_event
from soa_db.documents import (
    Document,
    DocumentRepository,
    SourceChannel,
)
from soa_db.jobs import enqueue_job
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_db.types import utcnow, uuid7
from soa_storage import ChecksumMismatchError, ObjectNotFoundError, SignedUrl, sha256_hex
from soa_storage.keys import artifact_key

router = APIRouter(tags=["uploads"])
logger = logging.getLogger(__name__)

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
    upload_headers: dict[str, str]
    expires_at: str
    state: str

    @classmethod
    def from_model(cls, record: UploadSession, *, signed: SignedUrl) -> "UploadSessionResponse":
        return cls(
            session_id=str(record.id),
            document_id=str(record.document_id),
            upload_url=signed.url,
            upload_method=signed.method,
            upload_headers=dict(signed.required_headers),
            expires_at=record.expires_at.isoformat(),
            state=record.state,
        )


class CompleteResponse(BaseModel):
    document_id: str
    state: str
    #: Why the document sits in an exceptional state (safe message).
    state_reason: str | None = None
    #: Set when the document duplicates an earlier one (ING-006).
    duplicate_of: str | None = None

    @classmethod
    def from_document(cls, document: "Document") -> "CompleteResponse":
        return cls(
            document_id=str(document.id),
            state=document.state,
            state_reason=document.state_reason,
            duplicate_of=str(document.duplicate_of) if document.duplicate_of else None,
        )


def _actor(authorized: AuthorizedContext) -> str:
    return f"user:{authorized.membership.user_id}"


async def _runtime_pins_by_stream_id(
    session: DbSession, authorized: AuthorizedContext, stream_id: uuid.UUID
) -> RuntimePins:
    """Resolve a complete immutable execution contract for the stream."""
    stream = await StreamRepository(session, authorized.org_context).get(stream_id)
    if stream is None:
        raise RuntimePinError("stream not found")
    return await resolve_runtime_pins(
        session,
        authorized.org_context,
        stream_version_id=stream.active_version_id,
    )


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
    # Abuse control (SEC-003): per-principal cap on session creation.
    await deps.rate_limiter.enforce(
        "uploads",
        f"user:{authorized.membership.user_id}",
        deps.settings.rate_limit_uploads_per_minute,
    )
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
    try:
        pins = await _runtime_pins_by_stream_id(session, authorized, stream.id)
    except RuntimePinError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    limits = resolve_limits(platform_limits, pins.config)

    repo = UploadSessionRepository(session, authorized.org_context)
    try:
        check_size(body.size_bytes, limits)
        # Count and create are one serialized tenant decision in PostgreSQL;
        # concurrent declarations cannot both observe the last free slot.
        await repo.lock_pending_quota()
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
    # The storage capability must never outlive the database session that
    # owns it. Session creation happens before URL signing, so reusing the
    # configured TTL here would leave a small post-expiry window in which a
    # client could recreate bytes after the cleanup job deleted them.
    signed_ttl_seconds = max(1, int((record.expires_at - utcnow()).total_seconds()))
    signed = await store.signed_upload_url(
        key,
        expires_in_seconds=signed_ttl_seconds,
        content_type=body.content_type,
        size_bytes=body.size_bytes,
        sha256=body.sha256,
    )
    cleanup_payload = {
        "organization_id": str(authorized.org_context.organization_id),
        "upload_session_id": str(record.id),
    }
    for pass_name, grace in (
        ("primary", UPLOAD_CLEANUP_GRACE),
        ("sweep", UPLOAD_CLEANUP_SWEEP_GRACE),
    ):
        await enqueue_job(
            session,
            job_type=UPLOAD_CLEANUP_JOB_TYPE,
            payload=cleanup_payload,
            organization_id=authorized.org_context.organization_id,
            dedupe_key=f"upload-session:{record.id}:cleanup:{pass_name}",
            run_after=record.expires_at + grace,
            max_attempts=8,
        )
    return UploadSessionResponse.from_model(record, signed=signed)


async def _load_session(
    session: DbSession, authorized: AuthorizedContext, session_id: uuid.UUID
) -> UploadSession:
    # Completion and abort both read-modify-write the same session and may be
    # delivered concurrently after a client retry. Serialize them so exactly
    # one request can register the preallocated document or delete its object.
    record = await UploadSessionRepository(session, authorized.org_context).get(
        session_id, for_update=True
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found."
        )
    if record.state == UploadSessionState.PENDING.value and is_expired(record):
        record.state = UploadSessionState.EXPIRED.value
        await session.flush()
        # HTTPException rolls the request unit of work back. Expiry is an
        # irreversible lifecycle fact, so persist this isolated transition
        # before returning 410. No endpoint mutation occurs before this load.
        await session.commit()
    if record.state == UploadSessionState.EXPIRED.value:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="This upload session has expired."
        )
    return record


async def _release_verification_claim(
    session: DbSession,
    organization_id: uuid.UUID,
    session_id: uuid.UUID,
    token: uuid.UUID,
) -> None:
    """Return this attempt's live claim to pending after a safe failure."""

    await session.rollback()
    context = OrganizationContext(organization_id=organization_id)
    await bind_tenant(session, organization_id)
    record = await UploadSessionRepository(session, context).get(
        session_id,
        for_update=True,
    )
    if (
        record is not None
        and record.state == UploadSessionState.VERIFYING.value
        and record.verification_token == token
    ):
        record.state = UploadSessionState.PENDING.value
        record.verification_token = None
        record.verification_started_at = None
        await session.flush()
        # Persist the release even though the endpoint returns an HTTP error
        # and the surrounding request unit of work consequently rolls back.
        await session.commit()


@router.post("/orgs/{organization_slug}/uploads/{session_id}/complete")
async def complete_upload(
    session_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.upload"))],
    session: DbSession,
    store: ObjectStoreDep,
    scanner: MalwareScannerDep,
) -> CompleteResponse:
    # Preserve the primitive tenant identity across explicit commits and
    # failure rollbacks. ORM-backed authorization objects may be expired by a
    # rollback and must not be lazily reloaded while releasing the lease.
    organization_id = authorized.org_context.organization_id
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
        return CompleteResponse.from_document(existing)
    if record.state == UploadSessionState.ABORTED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This upload session was aborted."
        )

    now = utcnow()
    if (
        record.state == UploadSessionState.VERIFYING.value
        and record.verification_started_at is not None
        and record.verification_started_at + UPLOAD_VERIFICATION_LEASE > now
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This upload session is already being verified; retry shortly.",
        )
    verification_token = uuid7()
    record.state = UploadSessionState.VERIFYING.value
    record.verification_started_at = now
    record.verification_token = verification_token
    await session.flush()
    # The durable claim replaces a row lock during storage and scanner I/O.
    await session.commit()

    # Verify the stored object against the declaration.
    try:
        metadata = await store.head(record.object_key)
    except ObjectNotFoundError:
        await _release_verification_claim(session, organization_id, session_id, verification_token)
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
        await _release_verification_claim(session, organization_id, session_id, verification_token)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="; ".join(problems)
        )

    try:
        data = await store.get(record.object_key)
    except ChecksumMismatchError:
        await _release_verification_claim(session, organization_id, session_id, verification_token)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Stored bytes do not match the declared SHA-256.",
        ) from None
    # Object metadata is a useful early check but is not the trust boundary:
    # a non-browser client or storage adapter may omit custom metadata. Hash
    # the exact bytes that will be scanned and registered so same-size altered
    # uploads can never enter the database under a false content identity.
    actual_sha256 = sha256_hex(data)
    if actual_sha256 != record.declared_sha256:
        await _release_verification_claim(session, organization_id, session_id, verification_token)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Stored bytes do not match the declared SHA-256.",
        )
    declaration = IntakeDeclaration(
        stream_id=record.stream_id,
        stream_config={},  # replaced with the authenticated pin below
        source_channel=SourceChannel.UPLOAD,
        filename=record.declared_filename,
        content_type=record.declared_content_type,
        sha256=record.declared_sha256,
        size_bytes=record.declared_size_bytes,
        object_key=record.object_key,
        client_reference=record.client_reference,
        source_metadata={"uploader": _actor(authorized)},
        document_id=record.document_id,
    )
    safety = await evaluate_intake_safety(declaration, data, scanner)

    await bind_tenant(session, authorized.org_context.organization_id)
    session.expire(record)
    locked = await UploadSessionRepository(session, authorized.org_context).get(
        session_id,
        for_update=True,
    )
    if (
        locked is None
        or locked.state != UploadSessionState.VERIFYING.value
        or locked.verification_token != verification_token
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This upload verification claim is no longer current.",
        )
    record = locked

    try:
        parent_stream = await StreamRepository(session, authorized.org_context).get(
            record.stream_id
        )
        pins = await _runtime_pins_by_stream_id(session, authorized, record.stream_id)
        document = await finalize_document_intake(
            session,
            authorized.org_context,
            declaration=IntakeDeclaration(
                stream_id=record.stream_id,
                stream_config=pins.config,
                source_channel=SourceChannel.UPLOAD,
                filename=record.declared_filename,
                content_type=record.declared_content_type,
                sha256=record.declared_sha256,
                size_bytes=record.declared_size_bytes,
                object_key=record.object_key,
                client_reference=record.client_reference,
                source_metadata={"uploader": _actor(authorized)},
                document_id=record.document_id,
                stream_version_id=(parent_stream.active_version_id if parent_stream else None),
                config_fingerprint=pins.config_fingerprint,
            ),
            safety=safety,
            actor_id=_actor(authorized),
        )
        record.state = UploadSessionState.COMPLETED.value
        record.completed_at = utcnow()
        record.verification_token = None
        record.verification_started_at = None
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
    except RuntimePinError as error:
        await _release_verification_claim(session, organization_id, session_id, verification_token)
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    except BaseException:
        try:
            await _release_verification_claim(
                session, organization_id, session_id, verification_token
            )
        except Exception:
            logger.exception("could not release a failed upload verification claim")
        raise
    return CompleteResponse.from_document(document)


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
    if record.state == UploadSessionState.VERIFYING.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An upload verification is in progress; retry abort shortly.",
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
