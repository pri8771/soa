"""Public API ingestion (ING-013).

Machine clients ingest documents with a service credential (TEN-009):

    POST /v1/streams/{stream_slug}/documents
    X-Api-Key: soa_<prefix>_<secret>
    multipart/form-data: file=<binary>, client_reference=<optional>

The key's organization decides the tenant — there is no organization in
the URL, so a key can never reach another tenant's streams by name.
The ``documents.upload`` scope is required; unknown or missing scopes
fail closed. Requests run the full shared intake pipeline (limits,
signature inspection, malware scan, duplicate policy, atomic
registration), so the API door applies exactly the same rules as the
interactive one.

Idempotency: a repeated ``client_reference`` returns HTTP 200 with the
ORIGINAL document — the safe duplicate response — instead of creating a
second delivery or erroring.

Rate control: a per-credential sliding-window limit
(``api_ingest_rate_per_minute``) answers 429 with Retry-After. The
window lives in process memory — honest for a single API instance;
multi-instance deployments need a shared store (REL epic).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel

from soa_api.auth.errors import AuthenticationError
from soa_api.dependencies import (
    DbSession,
    Dependencies,
    MalwareScannerDep,
    ObjectStoreDep,
    get_dependencies,
)
from soa_api.domain.credentials import (
    ScopeError,
    authenticate_api_key,
    credential_organization_id,
    require_scope,
)
from soa_api.domain.streams import StreamRepository, StreamStatus, StreamVersionRepository
from soa_api.domain.uploads import SUPPORTED_UPLOAD_TYPES
from soa_api.services.file_limits import FileLimits, LimitViolation, check_size, resolve_limits
from soa_api.services.ingestion import IntakeDeclaration, finalize_document_intake
from soa_db.audit import ActorType
from soa_db.documents import DocumentRepository, SourceChannel
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_db.types import uuid7
from soa_storage import sha256_hex
from soa_storage.keys import artifact_key

router = APIRouter(tags=["public-api"])

API_KEY_HEADER = "X-Api-Key"


class IngestResponse(BaseModel):
    document_id: str
    state: str
    state_reason: str | None = None
    duplicate_of: str | None = None
    #: True when this response replays an earlier ingestion for the same
    #: client_reference instead of creating a new document.
    idempotent_replay: bool = False


async def _authenticate(request: Request, session: DbSession) -> tuple[OrganizationContext, str]:
    raw_key = request.headers.get(API_KEY_HEADER)
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Provide a service credential in the {API_KEY_HEADER} header.",
        )
    try:
        principal = await authenticate_api_key(session, raw_key)
    except AuthenticationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from None
    try:
        require_scope(principal, "documents.upload")
        organization_id = credential_organization_id(principal)
    except ScopeError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from None
    context = OrganizationContext(organization_id=organization_id)
    await bind_tenant(session, organization_id)
    return context, f"api_key:{principal.subject}"


@router.post("/v1/streams/{stream_slug}/documents", status_code=status.HTTP_201_CREATED)
async def ingest_document(
    stream_slug: str,
    request: Request,
    file: UploadFile,
    session: DbSession,
    store: ObjectStoreDep,
    scanner: MalwareScannerDep,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
    client_reference: Annotated[str | None, Form(max_length=200)] = None,
) -> Any:
    context, actor_id = await _authenticate(request, session)

    # Abuse control (SEC-003): per-credential cap via the shared limiter.
    deps.rate_limiter.enforce("api_ingest", actor_id, deps.settings.api_ingest_rate_per_minute)

    stream = await StreamRepository(session, context).get_by_slug(stream_slug)
    if stream is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stream not found.")
    if stream.status == StreamStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This stream is archived and no longer accepts documents.",
        )

    # Safe duplicate response: the same client_reference replays the
    # original registration instead of erroring or double-delivering.
    if client_reference is not None:
        existing = await DocumentRepository(session, context).get_by_client_reference(
            stream.id, client_reference
        )
        if existing is not None:
            response = IngestResponse(
                document_id=str(existing.id),
                state=existing.state,
                state_reason=existing.state_reason,
                duplicate_of=str(existing.duplicate_of) if existing.duplicate_of else None,
                idempotent_replay=True,
            )
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=status.HTTP_200_OK, content=response.model_dump())

    data = await file.read()
    content_type = file.content_type or "application/octet-stream"
    filename = file.filename or "document"
    if content_type not in SUPPORTED_UPLOAD_TYPES:
        supported = ", ".join(sorted(SUPPORTED_UPLOAD_TYPES))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unsupported content type {content_type!r}; supported: {supported}",
        )

    # Effective limits: platform maxima, lowered by stream configuration.
    stream_config: dict[str, Any] = {}
    if stream.active_version_id is not None:
        active = await StreamVersionRepository(session, context).get(stream.active_version_id)
        if active is not None and active.resolved_snapshot:
            config = active.resolved_snapshot.get("config")
            if isinstance(config, dict):
                stream_config = config
    limits = resolve_limits(
        FileLimits(
            max_size_bytes=deps.settings.max_upload_bytes,
            max_pages=deps.settings.max_pages_per_document,
            max_total_pixels=deps.settings.max_total_pixels,
            max_decompressed_bytes=deps.settings.max_decompressed_bytes,
            max_conversion_seconds=deps.settings.max_conversion_seconds,
        ),
        stream_config,
    )
    try:
        check_size(len(data), limits)
    except LimitViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="empty file")

    document_id = uuid7()
    key = artifact_key(context.organization_id, document_id, kind="original", filename=filename)
    digest = sha256_hex(data)
    await store.put(key, data, content_type=content_type, sha256=digest)

    document = await finalize_document_intake(
        session,
        context,
        declaration=IntakeDeclaration(
            stream_id=stream.id,
            stream_config=stream_config,
            source_channel=SourceChannel.API,
            filename=filename,
            content_type=content_type,
            sha256=digest,
            size_bytes=len(data),
            object_key=key,
            client_reference=client_reference,
            source_metadata={"api_client": actor_id},
            document_id=document_id,
            stream_version_id=stream.active_version_id,
            config_fingerprint=(
                str(stream_config["fingerprint"]) if "fingerprint" in stream_config else None
            ),
        ),
        data=data,
        scanner=scanner,
        actor_id=actor_id,
        actor_type=ActorType.SERVICE,
    )
    return IngestResponse(
        document_id=str(document.id),
        state=document.state,
        state_reason=document.state_reason,
        duplicate_of=str(document.duplicate_of) if document.duplicate_of else None,
    )
